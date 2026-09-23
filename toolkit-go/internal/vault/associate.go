package vault

import (
	"sort"
	"time"
)

var (
	timelineTypes = map[string]struct{}{"message": {}, "event": {}, "span": {}, "turn": {}}
	// Fixed precedence at each object: canonical session field first, then
	// remaining hint names. Walk remaining keys in sorted order.
	activeHintPrecedence = []string{"active_message_id", "active_node_id", "current_node_id", "currentId"}
)

type Session struct {
	Key              RecordKey
	RecordKeys       []RecordKey
	MessageKeys      []RecordKey
	RelationshipKeys []RecordKey // parent_child used for topology
	LiteralRelKeys   []RecordKey
	TimelineKeys     []RecordKey
	ActiveNode       string
	CreatedAt        string
	UpdatedAt        string
	Title            *string
	Model            any
	AdapterNS        string
	SourceScope      string
	Topology         Topology
}

func projectSessions(arch *Archive, lim Limits) ([]Session, error) {
	associations := associateRecords(arch)
	var sessionKeys []RecordKey
	for key, rec := range arch.Records {
		if rec.Type == "session" {
			sessionKeys = append(sessionKeys, key)
		}
	}
	sessionKeys = sortedKeys(sessionKeys)
	if len(sessionKeys) > lim.MaxSessions {
		return nil, errc(LimitExceeded, 1)
	}
	out := make([]Session, 0, len(sessionKeys))
	for _, sk := range sessionKeys {
		sid := sk.Entity
		var members []RecordKey
		for key, sessions := range associations {
			if len(sessions) == 1 && sessions[0] == sid {
				members = append(members, key)
			}
		}
		members = sortedKeys(members)
		var messages, rels, timeline []RecordKey
		messageIDs := map[string]struct{}{}
		for _, key := range members {
			rec := arch.Records[key]
			switch rec.Type {
			case "message":
				messages = append(messages, key)
				messageIDs[key.Entity] = struct{}{}
			case "relationship":
				rels = append(rels, key)
			}
			if _, ok := timelineTypes[rec.Type]; ok {
				timeline = append(timeline, key)
			}
		}
		messages = sortedKeys(messages)
		rels = sortedKeys(rels)
		sort.Slice(timeline, func(i, j int) bool {
			a, b := arch.Records[timeline[i]], arch.Records[timeline[j]]
			oa, aOK := jsonIntValue(a.Body["source_ordinal"])
			ob, bOK := jsonIntValue(b.Body["source_ordinal"])
			if aOK != bOK {
				return aOK && !bOK
			}
			if aOK {
				if c := oa.Cmp(ob); c != 0 {
					return c < 0
				}
			}
			if a.Type != b.Type {
				return a.Type < b.Type
			}
			return timeline[i].Less(timeline[j])
		})
		var topoRels []RecordKey
		for key, rec := range arch.Records {
			if rec.Type != "relationship" {
				continue
			}
			kind, _ := asString(rec.Body["kind"])
			if kind != "parent_child" {
				continue
			}
			from, _ := asString(rec.Body["from_id"])
			to, _ := asString(rec.Body["to_id"])
			_, fromOK := messageIDs[from]
			_, toOK := messageIDs[to]
			if fromOK || toOK {
				topoRels = append(topoRels, key)
			}
		}
		topoRels = sortedKeys(topoRels)
		sess := arch.Records[sk]
		active, _ := sess.Body["active_message_id"].(string)
		if active == "" {
			active = resolveActive(arch, members, messageIDs, sess)
		}
		topo := projectTopology(arch, sk, messages, topoRels, active)
		created, _ := sess.Body["created_at"].(string)
		updated, _ := sess.Body["updated_at"].(string)
		var title *string
		if t, ok := sess.Body["title"].(string); ok {
			title = &t
		}
		out = append(out, Session{
			Key:              sk,
			RecordKeys:       members,
			MessageKeys:      messages,
			RelationshipKeys: topo.ParentChildKeys,
			LiteralRelKeys:   rels,
			TimelineKeys:     timeline,
			ActiveNode:       topo.ActiveNode,
			CreatedAt:        created,
			UpdatedAt:        updated,
			Title:            title,
			Model:            canonicalModel(sess.Body["model"]),
			AdapterNS:        objectStringOrEmpty(sess.Identity, "adapter_namespace"),
			SourceScope:      objectStringOrEmpty(sess.Identity, "source_scope_id"),
			Topology:         topo,
		})
	}
	sortSessions(out)
	return out, nil
}

func associateRecords(arch *Archive) map[RecordKey][]string {
	byEntity := map[string][]RecordKey{}
	for key := range arch.Records {
		byEntity[key.Entity] = append(byEntity[key.Entity], key)
	}
	sessions := map[string]struct{}{}
	for key, rec := range arch.Records {
		if rec.Type == "session" {
			sessions[key.Entity] = struct{}{}
		}
	}
	assoc := map[RecordKey]map[string]struct{}{}
	ensure := func(k RecordKey) map[string]struct{} {
		if assoc[k] == nil {
			assoc[k] = map[string]struct{}{}
		}
		return assoc[k]
	}
	for key, rec := range arch.Records {
		if rec.Type == "session" {
			ensure(key)[key.Entity] = struct{}{}
		} else if sid, ok := rec.Body["session_id"].(string); ok {
			if _, is := sessions[sid]; is {
				ensure(key)[sid] = struct{}{}
			}
		}
	}
	changed := true
	for changed {
		changed = false
		for key, rec := range arch.Records {
			candidates := map[string]struct{}{}
			switch rec.Type {
			case "observation":
				subj, _ := asObject(rec.Body["subject"])
				eid, _ := asString(subj["entity_id"])
				rid, _ := asString(subj["revision_id"])
				for s := range assoc[RecordKey{eid, rid}] {
					candidates[s] = struct{}{}
				}
			case "revision":
				eid, _ := asString(rec.Body["entity_id"])
				from, _ := asString(rec.Body["from_revision_id"])
				to, _ := asString(rec.Body["to_revision_id"])
				for s := range assoc[RecordKey{eid, from}] {
					candidates[s] = struct{}{}
				}
				for s := range assoc[RecordKey{eid, to}] {
					candidates[s] = struct{}{}
				}
			case "relationship":
				for _, end := range []string{
					objectStringOrEmpty(rec.Body, "from_id"),
					objectStringOrEmpty(rec.Body, "to_id"),
				} {
					for _, ek := range byEntity[end] {
						for s := range assoc[ek] {
							candidates[s] = struct{}{}
						}
					}
				}
			}
			if len(candidates) != 1 {
				continue
			}
			set := ensure(key)
			for s := range candidates {
				if _, ok := set[s]; !ok {
					set[s] = struct{}{}
					changed = true
				}
			}
		}
	}
	out := map[RecordKey][]string{}
	for key, set := range assoc {
		ids := make([]string, 0, len(set))
		for s := range set {
			ids = append(ids, s)
		}
		sort.Strings(ids)
		out[key] = ids
	}
	return out
}

func hintFrom(v any) string {
	switch t := v.(type) {
	case map[string]any:
		for _, k := range activeHintPrecedence {
			if item, ok := t[k]; ok {
				if s, ok := item.(string); ok && s != "" {
					return s
				}
			}
		}
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			if found := hintFrom(t[k]); found != "" {
				return found
			}
		}
	case []any:
		for _, item := range t {
			if found := hintFrom(item); found != "" {
				return found
			}
		}
	}
	return ""
}

func resolveActive(arch *Archive, members []RecordKey, messageIDs map[string]struct{}, session Record) string {
	hint := hintFrom(session.Extensions)
	if hint == "" {
		for _, key := range members {
			rec := arch.Records[key]
			if rec.Type == "event" {
				kind, _ := rec.Body["kind"].(string)
				if kind == "lifecycle" {
					hint = hintFrom(rec.Extensions)
					if hint != "" {
						break
					}
				}
			}
		}
	}
	if hint == "" {
		return ""
	}
	if _, ok := messageIDs[hint]; ok {
		return hint
	}
	sessionKey, _ := session.Identity["logical_key"].(string)
	prefix := ""
	if sessionKey != "" {
		prefix = sessionKey + ":"
	}
	for _, key := range members {
		rec := arch.Records[key]
		if rec.Type != "message" {
			continue
		}
		logical, _ := rec.Identity["logical_key"].(string)
		if logical == hint || (prefix != "" && stringsHasSuffixAfterPrefix(logical, prefix, hint)) {
			return rec.Entity
		}
	}
	return hint
}

func stringsHasSuffixAfterPrefix(logical, prefix, hint string) bool {
	if len(logical) < len(prefix) || logical[:len(prefix)] != prefix {
		return false
	}
	return logical[len(prefix):] == hint
}

func canonicalModel(v any) any {
	m, ok := asObject(v)
	if !ok {
		return nil
	}
	provider, ok1 := asString(m["provider"])
	name, ok2 := asString(m["model_name"])
	if !ok1 || !ok2 || provider == "" || name == "" {
		return nil
	}
	out := map[string]any{"model_name": name, "provider": provider}
	if rev, ok := asString(m["model_revision"]); ok && rev != "" {
		out["model_revision"] = rev
	}
	return out
}

func declaredShape(body map[string]any) any {
	s, ok := body["thread_shape"].(string)
	if !ok {
		return nil
	}
	switch s {
	case "linear", "tree", "unknown":
		return s
	default:
		return nil
	}
}

func sortSessions(sessions []Session) {
	sort.SliceStable(sessions, func(i, j int) bool {
		a, b := sessions[i], sessions[j]
		aMissing := a.CreatedAt == ""
		bMissing := b.CreatedAt == ""
		if aMissing != bMissing {
			return !aMissing && bMissing
		}
		if !aMissing {
			ta, ea := parseTime(a.CreatedAt)
			tb, eb := parseTime(b.CreatedAt)
			if ea && eb && !ta.Equal(tb) {
				return ta.Before(tb)
			}
			if a.CreatedAt != b.CreatedAt {
				return a.CreatedAt < b.CreatedAt
			}
		}
		if a.Key.Entity != b.Key.Entity {
			return a.Key.Entity < b.Key.Entity
		}
		return a.Key.Revision < b.Key.Revision
	})
}

func parseTime(s string) (time.Time, bool) {
	if t, err := time.Parse(time.RFC3339Nano, s); err == nil {
		return t, true
	}
	if t, err := time.Parse(time.RFC3339, s); err == nil {
		return t, true
	}
	return time.Time{}, false
}
