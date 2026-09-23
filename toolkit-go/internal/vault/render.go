package vault

import (
	"sort"
	"strconv"
	"strings"
)

func renderIndex(archiveID string, sessions []Session) ([]byte, error) {
	fm := map[string]any{
		"archive_id":   archiveID,
		"format":       FormatPage,
		"generated_by": GeneratedBy,
		"page_kind":    "index",
		"sensitivity":  Sensitivity,
		"spec_version": SpecVersion,
		"warning":      WarningText,
	}
	var b strings.Builder
	if err := writeFrontmatter(&b, fm); err != nil {
		return nil, err
	}
	b.WriteString(WarningText)
	b.WriteByte('\n')
	b.WriteString("\n# Archive index\n\n")
	b.WriteString("Chat pages are named only from canonical entity and revision digests.\n")
	b.WriteString("Missing created_at values sort last. source_ordinal is source order, not chronology.\n\n")
	b.WriteString("## Chats\n\n")
	if len(sessions) == 0 {
		b.WriteString("No session records.\n")
		return []byte(b.String()), nil
	}
	for _, s := range sessions {
		target, err := wikilinkTarget(s.Key)
		if err != nil {
			return nil, err
		}
		b.WriteString("- [[")
		b.WriteString(target)
		b.WriteString("]]\n")
		created := s.CreatedAt
		if created == "" {
			created = "unknown"
		}
		b.WriteString("  - created_at: ")
		b.WriteString(fence("text", created))
		b.WriteString("  - title:\n\n")
		title := ""
		if s.Title != nil {
			title = *s.Title
		}
		b.WriteString(indentFence(title))
		b.WriteByte('\n')
	}
	return []byte(b.String()), nil
}

func renderChat(archiveID string, arch *Archive, s Session) ([]byte, error) {
	if _, ok := digestHex(s.Key.Entity); !ok {
		return nil, errc(RecordInvalid, 1)
	}
	if _, ok := digestHex(s.Key.Revision); !ok {
		return nil, errc(RecordInvalid, 1)
	}
	var title any
	if s.Title != nil {
		title = *s.Title
	}
	var created any
	if s.CreatedAt != "" {
		created = s.CreatedAt
	}
	var updated any
	if s.UpdatedAt != "" {
		updated = s.UpdatedAt
	}
	fm := map[string]any{
		"adapter_namespace":     s.AdapterNS,
		"archive_id":            archiveID,
		"created_at":            created,
		"declared_thread_shape": s.Topology.DeclaredShape,
		"derived_shape":         s.Topology.DerivedShape,
		"entity_id":             s.Key.Entity,
		"format":                FormatPage,
		"generated_by":          GeneratedBy,
		"model":                 s.Model,
		"page_kind":             "chat",
		"revision_id":           s.Key.Revision,
		"sensitivity":           Sensitivity,
		"source_scope_id":       s.SourceScope,
		"spec_version":          SpecVersion,
		"title":                 title,
		"topology_basis":        s.Topology.Basis,
		"updated_at":            updated,
		"warning":               WarningText,
	}
	var b strings.Builder
	if err := writeFrontmatter(&b, fm); err != nil {
		return nil, err
	}
	b.WriteString(WarningText)
	b.WriteByte('\n')
	b.WriteString("\n# Chat\n\n")
	b.WriteString("## Literal title\n\n")
	titleText := ""
	if s.Title != nil {
		titleText = *s.Title
	}
	b.WriteString(fence("text", titleText))
	b.WriteByte('\n')
	b.WriteString("## Topology\n\n")
	b.WriteString("- declared_thread_shape is the canonical session field (`linear` / `tree` / `unknown`) or null.\n")
	b.WriteString("- derived_shape is inspector-style projection; `forest` is not a v0.1 canonical enum.\n")
	b.WriteString("- topology_basis states whether complete reciprocal parent_child evidence supports the derived shape.\n")
	b.WriteString("- Parent_child relationships are the only graph evidence. The source-selected active node does not drop branches.\n\n")
	b.WriteString("declared_thread_shape: ")
	b.WriteString(fence("json", mustCanon(s.Topology.DeclaredShape)))
	b.WriteString("derived_shape: ")
	b.WriteString(fence("text", s.Topology.DerivedShape))
	b.WriteString("topology_basis: ")
	b.WriteString(fence("text", s.Topology.Basis))
	b.WriteByte('\n')
	b.WriteString("## Parent-child graph\n\n")
	if err := writeGraph(&b, arch, s); err != nil {
		return nil, err
	}
	b.WriteString("\n## Source-ordinal timeline\n\n")
	b.WriteString("This section is labeled source order (`source_ordinal`). It is not chronology. Missing model or time remain unknown.\n\n")
	for _, key := range s.TimelineKeys {
		rec := arch.Records[key]
		if rec.Type == "message" {
			if err := writeMessage(&b, rec); err != nil {
				return nil, err
			}
		} else {
			if err := writeRecordBlock(&b, rec); err != nil {
				return nil, err
			}
		}
	}
	shown := map[RecordKey]struct{}{s.Key: {}}
	for _, k := range s.TimelineKeys {
		shown[k] = struct{}{}
	}
	for _, k := range s.LiteralRelKeys {
		shown[k] = struct{}{}
	}
	b.WriteString("## Literal relationships\n\n")
	if len(s.LiteralRelKeys) == 0 {
		b.WriteString("None.\n\n")
	} else {
		for _, k := range s.LiteralRelKeys {
			if err := writeRecordBlock(&b, arch.Records[k]); err != nil {
				return nil, err
			}
		}
	}
	var other []RecordKey
	for _, k := range s.RecordKeys {
		if _, ok := shown[k]; ok {
			continue
		}
		if arch.Records[k].Type == "observation" {
			continue
		}
		other = append(other, k)
	}
	other = sortedKeys(other)
	b.WriteString("## Other associated canonical records\n\n")
	if len(other) == 0 {
		b.WriteString("None.\n")
	} else {
		for _, k := range other {
			if err := writeRecordBlock(&b, arch.Records[k]); err != nil {
				return nil, err
			}
		}
	}
	return []byte(b.String()), nil
}

func writeFrontmatter(b *strings.Builder, obj map[string]any) error {
	raw, err := FrontmatterBytes(obj)
	if err != nil {
		return err
	}
	b.WriteString("---\n")
	b.Write(raw)
	b.WriteString("\n---\n\n")
	return nil
}

// canonicalRecordTypes/canonicalRoles are renderer allowlists for unfenced
// Markdown interpolation. Rejecting other values is interpolation safety, not
// Draft 2020-12 JSON Schema validation.
var canonicalRecordTypes = map[string]struct{}{
	"source_artifact": {}, "import_batch": {}, "session": {}, "turn": {},
	"message": {}, "event": {}, "span": {}, "artifact": {}, "relationship": {},
	"revision": {}, "observation": {}, "proposal": {},
}

var canonicalRoles = map[string]struct{}{
	"user": {}, "assistant": {}, "system": {}, "developer": {}, "tool": {}, "unknown": {},
}

func requireEnum(val string, allowed map[string]struct{}) error {
	if _, ok := allowed[val]; !ok {
		return errc(RecordInvalid, 1)
	}
	return nil
}

func safeHeadingToken(s string) bool {
	if s == "" || len(s) > 64 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' {
			continue
		}
		return false
	}
	return true
}

func mustCanon(v any) string {
	raw, err := CanonicalBytes(v)
	if err != nil {
		return "null"
	}
	return string(raw)
}

func fence(lang, content string) string {
	n := maxBacktickRun(content) + 1
	if n < 3 {
		n = 3
	}
	ticks := strings.Repeat("`", n)
	var b strings.Builder
	b.WriteString(ticks)
	if lang != "" {
		b.WriteString(lang)
	}
	b.WriteByte('\n')
	b.WriteString(content)
	if !strings.HasSuffix(content, "\n") {
		b.WriteByte('\n')
	}
	b.WriteString(ticks)
	b.WriteByte('\n')
	return b.String()
}

func indentFence(content string) string {
	return fence("text", content)
}

func maxBacktickRun(s string) int {
	max, run := 0, 0
	for i := 0; i < len(s); i++ {
		if s[i] == '`' {
			run++
			if run > max {
				max = run
			}
		} else {
			run = 0
		}
	}
	return max
}

func writeGraph(b *strings.Builder, arch *Archive, s Session) error {
	if len(s.MessageKeys) == 0 {
		b.WriteString("No messages.\n")
		return nil
	}
	if s.Topology.Basis == "revision_ambiguous" {
		b.WriteString("Graph rendering stopped: parent-child graph evidence has ambiguous revisions. ")
		b.WriteString("Each message revision remains available as its own timeline card; no arbitrary revision lookup was built.\n")
		return nil
	}
	if s.Topology.Basis == "incomplete_or_unknown" || s.Topology.Basis == "invalid_shape" {
		b.WriteString("Derived topology is unknown or invalid (")
		b.WriteString(s.Topology.Basis)
		b.WriteString("). Every associated message is listed rather than inventing links.\n\n")
	}
	if s.Topology.DerivedShape == "forest" && s.Topology.Basis == "complete_parent_child" {
		b.WriteString("Confirmed derived forest. Labels are Thread A, Thread B, … Thread AA. ")
		b.WriteString("The source-selected active node does not drop branches.\n\n")
	}
	outgoing := map[string][]string{}
	incoming := map[string]struct{}{}
	lookup := map[string]RecordKey{}
	for _, k := range s.MessageKeys {
		lookup[k.Entity] = k
	}
	byEdge := map[[2]string]map[string]any{}
	for _, key := range s.RelationshipKeys {
		body := arch.Records[key].Body
		from, _ := asString(body["from_id"])
		to, _ := asString(body["to_id"])
		if _, ok := lookup[from]; !ok {
			continue
		}
		if _, ok := lookup[to]; !ok {
			continue
		}
		outgoing[from] = append(outgoing[from], to)
		incoming[to] = struct{}{}
		byEdge[[2]string{from, to}] = body
	}
	var fallbackParents []string
	for parent, children := range outgoing {
		okIdx := true
		seen := map[string]struct{}{}
		for _, child := range children {
			lex, ok := jsonIntLex(byEdge[[2]string{parent, child}]["sibling_index"])
			if !ok {
				okIdx = false
				break
			}
			if _, dup := seen[lex]; dup {
				okIdx = false
				break
			}
			seen[lex] = struct{}{}
		}
		if okIdx && len(seen) == len(children) {
			sort.Slice(children, func(i, j int) bool {
				ai, _ := jsonIntValue(byEdge[[2]string{parent, children[i]}]["sibling_index"])
				bi, _ := jsonIntValue(byEdge[[2]string{parent, children[j]}]["sibling_index"])
				return ai.Cmp(bi) < 0
			})
		} else {
			sort.Slice(children, func(i, j int) bool {
				oa, aOK := jsonIntValue(arch.Records[lookup[children[i]]].Body["source_ordinal"])
				ob, bOK := jsonIntValue(arch.Records[lookup[children[j]]].Body["source_ordinal"])
				if aOK != bOK {
					return aOK && !bOK
				}
				if aOK {
					if c := oa.Cmp(ob); c != 0 {
						return c < 0
					}
				}
				return children[i] < children[j]
			})
			fallbackParents = append(fallbackParents, parent)
		}
		outgoing[parent] = children
	}
	sort.Strings(fallbackParents)
	if len(fallbackParents) == 0 && len(outgoing) > 0 {
		b.WriteString("Child ordering uses complete, distinct sibling_index values.\n\n")
	} else if len(fallbackParents) > 0 {
		b.WriteString("Child ordering fallback (display-only) for parent(s) ")
		for i, p := range fallbackParents {
			if i > 0 {
				b.WriteString(", ")
			}
			b.WriteByte('`')
			b.WriteString(p)
			b.WriteByte('`')
		}
		b.WriteString(": sibling_index is incomplete or not distinct, so children sort by source ordinal then canonical entity ID.\n\n")
	}
	if s.Topology.DerivedShape == "forest" && s.Topology.Basis == "complete_parent_child" {
		for i, comp := range s.Topology.Components {
			label := componentLabel("forest", i)
			b.WriteString("### ")
			b.WriteString(label)
			b.WriteString("\n\n")
			b.WriteString("- messages: ")
			b.WriteString(strconv.Itoa(len(comp)))
			b.WriteByte('\n')
			activeHere := false
			for _, k := range comp {
				if k.Entity == s.ActiveNode {
					activeHere = true
					break
				}
			}
			if activeHere {
				b.WriteString("- contains active node: yes\n")
			}
			var roots []RecordKey
			rootSet := map[RecordKey]struct{}{}
			for _, r := range s.Topology.Roots {
				rootSet[r] = struct{}{}
			}
			for _, k := range comp {
				if _, ok := rootSet[k]; ok {
					roots = append(roots, k)
				}
			}
			b.WriteString("- roots:\n")
			for _, r := range roots {
				b.WriteString("  - `")
				b.WriteString(r.Entity)
				b.WriteString("`\n")
			}
			if err := writeTreeFrom(b, arch, lookup, outgoing, roots, s.ActiveNode, 0); err != nil {
				return err
			}
			b.WriteByte('\n')
		}
		return nil
	}
	var roots []string
	for _, k := range s.Topology.Roots {
		roots = append(roots, k.Entity)
	}
	if len(roots) == 0 {
		for _, k := range s.MessageKeys {
			if _, ok := incoming[k.Entity]; !ok {
				roots = append(roots, k.Entity)
			}
		}
	}
	if len(roots) == 0 {
		for _, k := range sortedKeys(s.MessageKeys) {
			roots = append(roots, k.Entity)
		}
	}
	var rootKeys []RecordKey
	for _, id := range roots {
		if k, ok := lookup[id]; ok {
			rootKeys = append(rootKeys, k)
		}
	}
	return writeTreeFrom(b, arch, lookup, outgoing, rootKeys, s.ActiveNode, 0)
}

func writeTreeFrom(b *strings.Builder, arch *Archive, lookup map[string]RecordKey, outgoing map[string][]string, roots []RecordKey, active string, indent int) error {
	seen := map[string]struct{}{}
	var nodeErr error
	var node func(id string, indent int)
	node = func(id string, indent int) {
		if nodeErr != nil {
			return
		}
		pad := strings.Repeat("  ", indent)
		if _, ok := seen[id]; ok {
			b.WriteString(pad)
			b.WriteString("- cycle `")
			b.WriteString(id)
			b.WriteString("`\n")
			return
		}
		seen[id] = struct{}{}
		rec := arch.Records[lookup[id]]
		role, _ := rec.Body["role"].(string)
		if err := requireEnum(role, canonicalRoles); err != nil {
			nodeErr = err
			return
		}
		ord := "unknown"
		if lex, ok := jsonIntLex(rec.Body["source_ordinal"]); ok {
			ord = lex
		}
		b.WriteString(pad)
		b.WriteString("- `")
		b.WriteString(id)
		b.WriteString("` role ")
		b.WriteString(role)
		b.WriteString(" source_ordinal ")
		b.WriteString(ord)
		b.WriteString(" (source order, not time)")
		if id == active {
			b.WriteString(" [active node]")
		}
		b.WriteByte('\n')
		for _, child := range outgoing[id] {
			node(child, indent+1)
		}
		delete(seen, id)
	}
	for _, r := range roots {
		node(r.Entity, indent)
	}
	return nodeErr
}

func writeMessage(b *strings.Builder, rec Record) error {
	b.WriteString("### Message\n\n")
	b.WriteString("- entity_id: `")
	b.WriteString(rec.Entity)
	b.WriteString("`\n- revision_id: `")
	b.WriteString(rec.Revision)
	b.WriteString("`\n")
	role, _ := rec.Body["role"].(string)
	vis, _ := rec.Body["visibility"].(string)
	ord := "unknown"
	if lex, ok := jsonIntLex(rec.Body["source_ordinal"]); ok {
		ord = lex
	}
	b.WriteString("- role: ")
	b.WriteString(fence("text", role))
	b.WriteString("- visibility: ")
	b.WriteString(fence("text", vis))
	b.WriteString("- source_ordinal (source order, not chronology): ")
	b.WriteString(fence("text", ord))
	created, _ := rec.Body["created_at"].(string)
	if created == "" {
		created = "unknown"
	}
	b.WriteString("- created_at: ")
	b.WriteString(fence("text", created))
	b.WriteString("- model: ")
	b.WriteString(fence("json", mustCanon(canonicalModel(rec.Body["model"]))))
	content, _ := asArray(rec.Body["content"])
	for _, part := range content {
		obj, ok := asObject(part)
		if !ok {
			b.WriteString("#### Literal content part\n\n")
			b.WriteString(fence("json", mustCanon(part)))
			b.WriteByte('\n')
			continue
		}
		kind, _ := asString(obj["type"])
		switch kind {
		case "text", "reasoning", "code":
			text, _ := obj["text"].(string)
			b.WriteString("#### Literal ")
			b.WriteString(kind)
			b.WriteString("\n\n")
			b.WriteString(fence("text", text))
			b.WriteByte('\n')
		default:
			// Do not interpolate unchecked type tokens into headings.
			b.WriteString("#### Literal content part\n\n")
			if kind != "" && !safeHeadingToken(kind) {
				return errc(RecordInvalid, 1)
			}
			b.WriteString(fence("json", mustCanon(obj)))
			b.WriteByte('\n')
		}
	}
	return nil
}

func writeRecordBlock(b *strings.Builder, rec Record) error {
	if err := requireEnum(rec.Type, canonicalRecordTypes); err != nil {
		return err
	}
	b.WriteString("### Canonical ")
	b.WriteString(rec.Type)
	b.WriteString("\n\n")
	b.WriteString("- entity_id: `")
	b.WriteString(rec.Entity)
	b.WriteString("`\n- revision_id: `")
	b.WriteString(rec.Revision)
	b.WriteString("`\n\n")
	b.WriteString(fence("json", mustCanon(rec.Raw)))
	b.WriteByte('\n')
	return nil
}
