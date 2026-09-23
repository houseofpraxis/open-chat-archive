package vault

import "sort"

type Topology struct {
	DeclaredShape   any
	DerivedShape    string
	Basis           string
	Components      [][]RecordKey
	Roots           []RecordKey
	Leaves          []RecordKey
	Depth           int
	ActiveNode      string
	ActiveChildren  []RecordKey
	ParentChildKeys []RecordKey
	Codes           []string
}

func projectTopology(arch *Archive, sessionKey RecordKey, messageKeys, parentKeys []RecordKey, activeNode string) Topology {
	session := arch.Records[sessionKey]
	declared := declaredShape(session.Body)
	messageIDs := map[string]struct{}{}
	for _, k := range messageKeys {
		messageIDs[k.Entity] = struct{}{}
	}
	emptyActive := ""
	if _, ok := messageIDs[activeNode]; ok {
		emptyActive = activeNode
	}
	baseEmpty := Topology{
		DeclaredShape: declared,
		DerivedShape:  "unknown",
		Basis:         "incomplete_or_unknown",
		ActiveNode:    emptyActive,
	}
	if revisionAmbiguous(arch, sessionKey, messageKeys, parentKeys) {
		baseEmpty.Basis = "revision_ambiguous"
		baseEmpty.Codes = []string{"REVISION_AMBIGUOUS"}
		return baseEmpty
	}
	if len(messageKeys) == 0 {
		baseEmpty.Codes = []string{"TOPOLOGY_PROOF_NO_MESSAGES"}
		return baseEmpty
	}
	outgoing, incoming, endpointProblem, duplicateEdges := canonicalEdges(arch, parentKeys, messageIDs)
	components := graphComponents(messageKeys, outgoing, incoming)
	roots := orderedKeys(messageKeys, func() []string {
		var ids []string
		for _, k := range messageKeys {
			if len(incoming[k.Entity]) == 0 {
				ids = append(ids, k.Entity)
			}
		}
		return ids
	}())
	leaves := orderedKeys(messageKeys, func() []string {
		var ids []string
		for _, k := range messageKeys {
			if len(outgoing[k.Entity]) == 0 {
				ids = append(ids, k.Entity)
			}
		}
		return ids
	}())
	cycle := hasCycle(messageIDs, outgoing)
	multiParent := false
	for _, parents := range incoming {
		if len(parents) > 1 {
			multiParent = true
			break
		}
	}
	activeChildren := orderedKeys(messageKeys, setToSlice(outgoing[activeNode]))
	base := Topology{
		DeclaredShape:   declared,
		DerivedShape:    "unknown",
		Basis:           "incomplete_or_unknown",
		Components:      components,
		Roots:           roots,
		Leaves:          leaves,
		Depth:           graphDepth(roots, outgoing),
		ActiveNode:      emptyActive,
		ActiveChildren:  activeChildren,
		ParentChildKeys: parentKeys,
	}
	if endpointProblem || duplicateEdges || cycle || multiParent {
		var codes []string
		if endpointProblem {
			codes = append(codes, "INVALID_PARENT_CHILD_ENDPOINT")
		}
		if duplicateEdges {
			codes = append(codes, "DUPLICATE_PARENT_CHILD_EDGE")
		}
		if cycle {
			codes = append(codes, "PARENT_CHILD_CYCLE")
		}
		if multiParent {
			codes = append(codes, "MULTIPLE_PARENT_CHILD_PARENTS")
		}
		base.Basis = "invalid_shape"
		base.Codes = codes
		return base
	}
	if proof := nativeProof(arch, sessionKey, messageKeys, parentKeys); proof != "" {
		base.Codes = []string{proof}
		return base
	}
	shape := "linear"
	if len(components) > 1 {
		shape = "forest"
	} else {
		for _, children := range outgoing {
			if len(children) > 1 {
				shape = "tree"
				break
			}
		}
	}
	base.DerivedShape = shape
	base.Basis = "complete_parent_child"
	return base
}

func revisionAmbiguous(arch *Archive, sessionKey RecordKey, messageKeys, parentKeys []RecordKey) bool {
	selected := map[RecordKey]struct{}{sessionKey: {}}
	for _, k := range messageKeys {
		selected[k] = struct{}{}
	}
	for _, k := range parentKeys {
		selected[k] = struct{}{}
	}
	entities := map[string]map[string]struct{}{}
	for key := range arch.Records {
		if entities[key.Entity] == nil {
			entities[key.Entity] = map[string]struct{}{}
		}
		entities[key.Entity][key.Revision] = struct{}{}
	}
	for key := range selected {
		if len(entities[key.Entity]) > 1 {
			return true
		}
	}
	return false
}

func canonicalEdges(arch *Archive, keys []RecordKey, messageIDs map[string]struct{}) (outgoing, incoming map[string]map[string]struct{}, endpointProblem, duplicate bool) {
	outgoing = map[string]map[string]struct{}{}
	incoming = map[string]map[string]struct{}{}
	type pair struct{ a, b string }
	seen := map[pair]struct{}{}
	var n int
	for _, key := range keys {
		body := arch.Records[key].Body
		left, _ := asString(body["from_id"])
		right, _ := asString(body["to_id"])
		if _, ok := messageIDs[left]; !ok {
			endpointProblem = true
			continue
		}
		if _, ok := messageIDs[right]; !ok {
			endpointProblem = true
			continue
		}
		n++
		seen[pair{left, right}] = struct{}{}
		if outgoing[left] == nil {
			outgoing[left] = map[string]struct{}{}
		}
		if incoming[right] == nil {
			incoming[right] = map[string]struct{}{}
		}
		outgoing[left][right] = struct{}{}
		incoming[right][left] = struct{}{}
	}
	return outgoing, incoming, endpointProblem, n != len(seen)
}

func nativeProof(arch *Archive, sessionKey RecordKey, messageKeys, parentKeys []RecordKey) string {
	session := arch.Records[sessionKey]
	ns, _ := session.Identity["adapter_namespace"].(string)
	prefix, _ := session.Identity["logical_key"].(string)
	if ns != OpenWebUINamespace || prefix == "" {
		return "TOPOLOGY_PROOF_UNAVAILABLE"
	}
	prefix += ":"
	native := map[string]RecordKey{}
	parents := map[string]any{}
	children := map[string][]string{}
	for _, key := range messageKeys {
		rec := arch.Records[key]
		logical, _ := rec.Identity["logical_key"].(string)
		msgNS, _ := rec.Identity["adapter_namespace"].(string)
		if msgNS != OpenWebUINamespace || logical == "" || len(logical) < len(prefix) || logical[:len(prefix)] != prefix {
			return "TOPOLOGY_PROOF_MESSAGE_IDENTITY"
		}
		nativeID := logical[len(prefix):]
		if nativeID == "" {
			return "TOPOLOGY_PROOF_MESSAGE_IDENTITY"
		}
		if _, exists := native[nativeID]; exists {
			return "TOPOLOGY_PROOF_MESSAGE_IDENTITY"
		}
		ext, _ := asObject(rec.Extensions[OpenWebUINamespace])
		if ext == nil {
			return "TOPOLOGY_PROOF_NATIVE_METADATA"
		}
		if _, ok := ext["parentId"]; !ok {
			return "TOPOLOGY_PROOF_NATIVE_METADATA"
		}
		if _, ok := ext["childrenIds"]; !ok {
			return "TOPOLOGY_PROOF_NATIVE_METADATA"
		}
		parent := ext["parentId"]
		if parent != nil {
			if _, ok := parent.(string); !ok {
				return "TOPOLOGY_PROOF_NATIVE_METADATA"
			}
		}
		childIDs, ok := asArray(ext["childrenIds"])
		if !ok {
			return "TOPOLOGY_PROOF_NATIVE_METADATA"
		}
		seenChild := map[string]struct{}{}
		ids := make([]string, 0, len(childIDs))
		for _, item := range childIDs {
			s, ok := item.(string)
			if !ok {
				return "TOPOLOGY_PROOF_NATIVE_METADATA"
			}
			if _, dup := seenChild[s]; dup {
				return "TOPOLOGY_PROOF_NATIVE_METADATA"
			}
			seenChild[s] = struct{}{}
			ids = append(ids, s)
		}
		native[nativeID] = key
		parents[nativeID] = parent
		children[nativeID] = ids
	}
	if len(native) != len(messageKeys) {
		return "TOPOLOGY_PROOF_MESSAGE_IDENTITY"
	}
	nativeSet := map[RecordKey]struct{}{}
	for _, k := range native {
		nativeSet[k] = struct{}{}
	}
	for _, k := range messageKeys {
		if _, ok := nativeSet[k]; !ok {
			return "TOPOLOGY_PROOF_MESSAGE_IDENTITY"
		}
	}
	expected := map[[2]string]struct{}{}
	for node, parent := range parents {
		if parent != nil {
			ps := parent.(string)
			if _, ok := native[ps]; !ok {
				return "TOPOLOGY_PROOF_NATIVE_RECIPROCITY"
			}
			found := false
			for _, c := range children[ps] {
				if c == node {
					found = true
					break
				}
			}
			if !found {
				return "TOPOLOGY_PROOF_NATIVE_RECIPROCITY"
			}
			expected[[2]string{ps, node}] = struct{}{}
		}
		for _, child := range children[node] {
			if _, ok := native[child]; !ok {
				return "TOPOLOGY_PROOF_NATIVE_RECIPROCITY"
			}
			if parents[child] != node {
				return "TOPOLOGY_PROOF_NATIVE_RECIPROCITY"
			}
		}
	}
	entityNative := map[string]string{}
	for node, key := range native {
		entityNative[key.Entity] = node
	}
	actual := map[[2]string]struct{}{}
	for _, key := range parentKeys {
		body := arch.Records[key].Body
		from, _ := asString(body["from_id"])
		to, _ := asString(body["to_id"])
		pair := [2]string{entityNative[from], entityNative[to]}
		if _, dup := actual[pair]; dup {
			return "TOPOLOGY_PROOF_CANONICAL_EDGES"
		}
		actual[pair] = struct{}{}
	}
	if len(actual) != len(expected) {
		return "TOPOLOGY_PROOF_CANONICAL_EDGES"
	}
	for p := range expected {
		if _, ok := actual[p]; !ok {
			return "TOPOLOGY_PROOF_CANONICAL_EDGES"
		}
	}
	return ""
}

func orderedKeys(keys []RecordKey, entityIDs []string) []RecordKey {
	lookup := map[string]RecordKey{}
	for _, k := range keys {
		lookup[k.Entity] = k
	}
	var out []RecordKey
	for _, id := range entityIDs {
		if k, ok := lookup[id]; ok {
			out = append(out, k)
		}
	}
	return sortedKeys(out)
}

func graphComponents(keys []RecordKey, outgoing, incoming map[string]map[string]struct{}) [][]RecordKey {
	lookup := map[string]RecordKey{}
	unseen := map[string]struct{}{}
	for _, k := range keys {
		lookup[k.Entity] = k
		unseen[k.Entity] = struct{}{}
	}
	var result [][]RecordKey
	for len(unseen) > 0 {
		start := minString(unseen)
		pending := []string{start}
		found := map[string]struct{}{}
		for len(pending) > 0 {
			node := pending[len(pending)-1]
			pending = pending[:len(pending)-1]
			if _, ok := found[node]; ok {
				continue
			}
			found[node] = struct{}{}
			delete(unseen, node)
			for nb := range outgoing[node] {
				if _, ok := found[nb]; !ok {
					pending = append(pending, nb)
				}
			}
			for nb := range incoming[node] {
				if _, ok := found[nb]; !ok {
					pending = append(pending, nb)
				}
			}
		}
		var comp []RecordKey
		for node := range found {
			comp = append(comp, lookup[node])
		}
		result = append(result, sortedKeys(comp))
	}
	sort.Slice(result, func(i, j int) bool {
		a, b := result[i], result[j]
		n := len(a)
		if len(b) < n {
			n = len(b)
		}
		for k := 0; k < n; k++ {
			if a[k] != b[k] {
				return a[k].Less(b[k])
			}
		}
		return len(a) < len(b)
	})
	return result
}

func hasCycle(nodes map[string]struct{}, outgoing map[string]map[string]struct{}) bool {
	visiting := map[string]struct{}{}
	visited := map[string]struct{}{}
	var visit func(string) bool
	visit = func(node string) bool {
		if _, ok := visiting[node]; ok {
			return true
		}
		if _, ok := visited[node]; ok {
			return false
		}
		visiting[node] = struct{}{}
		for child := range outgoing[node] {
			if visit(child) {
				return true
			}
		}
		delete(visiting, node)
		visited[node] = struct{}{}
		return false
	}
	for node := range nodes {
		if visit(node) {
			return true
		}
	}
	return false
}

func graphDepth(roots []RecordKey, outgoing map[string]map[string]struct{}) int {
	var walk func(string, map[string]struct{}) int
	walk = func(node string, seen map[string]struct{}) int {
		max := 0
		for child := range outgoing[node] {
			if _, ok := seen[child]; ok {
				continue
			}
			next := map[string]struct{}{}
			for k := range seen {
				next[k] = struct{}{}
			}
			next[node] = struct{}{}
			d := walk(child, next)
			if d > max {
				max = d
			}
		}
		return 1 + max
	}
	max := 0
	for _, r := range roots {
		d := walk(r.Entity, map[string]struct{}{})
		if d > max {
			max = d
		}
	}
	return max
}

func minString(set map[string]struct{}) string {
	first := true
	var m string
	for s := range set {
		if first || s < m {
			m = s
			first = false
		}
	}
	return m
}

func setToSlice(set map[string]struct{}) []string {
	if set == nil {
		return nil
	}
	out := make([]string, 0, len(set))
	for s := range set {
		out = append(out, s)
	}
	return out
}

func alphaLabel(index int) string {
	result := ""
	value := index + 1
	for value > 0 {
		value--
		result = string(rune('A'+value%26)) + result
		value /= 26
	}
	return result
}

func componentLabel(shape string, index int) string {
	prefix := "Component "
	if shape == "forest" {
		prefix = "Thread "
	}
	return prefix + alphaLabel(index)
}
