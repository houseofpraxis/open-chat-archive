package vault

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const (
	testNS    = "org.open-chat-archive.synthetic"
	testScope = "synthetic-scope"
	owuiNS    = OpenWebUINamespace
)

func ident(archiveID, rtype, key, ns string) map[string]any {
	return map[string]any{
		"adapter_namespace": ns,
		"archive_id":        archiveID,
		"identity_scheme":   "native-id",
		"identity_version":  "1",
		"logical_key":       key,
		"record_type":       rtype,
		"source_scope_id":   testScope,
	}
}

func makeRecord(t *testing.T, archiveID, rtype, key, ns string, body map[string]any, ext map[string]any) map[string]any {
	t.Helper()
	id := ident(archiveID, rtype, key, ns)
	eid, err := EntityID(id)
	if err != nil {
		t.Fatal(err)
	}
	rec := map[string]any{
		"body":         body,
		"entity_id":    eid,
		"identity":     id,
		"record_type":  rtype,
		"sensitivity":  Sensitivity,
		"spec_version": SpecVersion,
	}
	if ext != nil {
		rec["extensions"] = ext
	}
	rid, err := RevisionID(rec)
	if err != nil {
		t.Fatal(err)
	}
	rec["revision_id"] = rid
	return rec
}

func encodeRecords(t *testing.T, recs []map[string]any) []byte {
	t.Helper()
	type pair struct {
		rt, e, r string
		raw      []byte
	}
	var items []pair
	for _, rec := range recs {
		raw, err := CanonicalBytes(rec)
		if err != nil {
			t.Fatal(err)
		}
		items = append(items, pair{
			rt:  rec["record_type"].(string),
			e:   rec["entity_id"].(string),
			r:   rec["revision_id"].(string),
			raw: raw,
		})
	}
	for i := 0; i < len(items); i++ {
		for j := i + 1; j < len(items); j++ {
			a, b := items[i], items[j]
			if a.rt > b.rt || (a.rt == b.rt && (a.e > b.e || (a.e == b.e && a.r > b.r))) {
				items[i], items[j] = items[j], items[i]
			}
		}
	}
	var buf bytes.Buffer
	for _, it := range items {
		buf.Write(it.raw)
		buf.WriteByte('\n')
	}
	return buf.Bytes()
}

func chmodTree(t *testing.T, root string) {
	t.Helper()
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			return os.Chmod(path, 0o700)
		}
		return os.Chmod(path, 0o600)
	})
	if err != nil {
		t.Fatal(err)
	}
}

func writeArchive(t *testing.T, parent, archiveID, batchID string, recs []map[string]any, withSentinel bool) (archive, attestation string) {
	t.Helper()
	archive = filepath.Join(parent, archiveID)
	if err := os.MkdirAll(filepath.Join(archive, "batches", batchID), 0o700); err != nil {
		t.Fatal(err)
	}
	ndjson := encodeRecords(t, recs)
	recPath := filepath.Join(archive, "batches", batchID, "records.ndjson")
	if err := os.WriteFile(recPath, ndjson, 0o600); err != nil {
		t.Fatal(err)
	}
	man := map[string]any{
		"adapter":      map[string]any{"namespace": testNS, "version": "0.1.0"},
		"archive_id":   archiveID,
		"batch_id":     batchID,
		"blobs":        []any{},
		"profile":      "batch-ndjson",
		"records":      map[string]any{"byte_digest": sha256Digest(ndjson), "count": int64(len(recs)), "path": "records.ndjson", "size": int64(len(ndjson))},
		"sensitivity":  Sensitivity,
		"sources":      []any{},
		"spec_version": SpecVersion,
	}
	manRaw, err := CanonicalBytes(man)
	if err != nil {
		t.Fatal(err)
	}
	manRaw = append(manRaw, '\n')
	if err := os.WriteFile(filepath.Join(archive, "batches", batchID, "manifest.json"), manRaw, 0o600); err != nil {
		t.Fatal(err)
	}
	if withSentinel {
		src := filepath.Join(archive, "sources")
		blob := filepath.Join(archive, "blobs")
		if err := os.MkdirAll(src, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.MkdirAll(blob, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(src, "object-sentinel"), []byte("SENTINEL-SOURCE-BYTES-DO-NOT-COPY"), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(blob, "object-sentinel"), []byte("SENTINEL-BLOB-BYTES-DO-NOT-COPY"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	chmodTree(t, archive)
	if withSentinel {
		if err := os.Chmod(filepath.Join(archive, "sources"), 0o000); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(filepath.Join(archive, "blobs"), 0o000); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			_ = os.Chmod(filepath.Join(archive, "sources"), 0o700)
			_ = os.Chmod(filepath.Join(archive, "blobs"), 0o700)
		})
	}
	attObj := map[string]any{
		"archive_id":       archiveID,
		"attestation_kind": AttestationKind,
		"batches": []any{
			map[string]any{
				"batch_id": batchID,
				"manifest": map[string]any{
					"byte_digest": sha256Digest(manRaw),
					"path":        "batches/" + batchID + "/manifest.json",
					"size":        int64(len(manRaw)),
				},
				"records": map[string]any{
					"byte_digest": sha256Digest(ndjson),
					"count":       int64(len(recs)),
					"path":        "batches/" + batchID + "/records.ndjson",
					"size":        int64(len(ndjson)),
				},
			},
		},
		"format":       FormatAttestation,
		"spec_version": SpecVersion,
		"status":       StatusPassed,
		"validator": map[string]any{
			"name":            ValidatorName,
			"package_version": "0.1.0.dev0",
		},
	}
	attRaw, err := CanonicalBytes(attObj)
	if err != nil {
		t.Fatal(err)
	}
	attRaw = append(attRaw, '\n')
	attestation = filepath.Join(parent, "attestation.json")
	if err := os.WriteFile(attestation, attRaw, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(attestation, 0o600); err != nil {
		t.Fatal(err)
	}
	return archive, attestation
}

func collectFiles(t *testing.T, root string) map[string][]byte {
	t.Helper()
	out := map[string][]byte{}
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			return nil
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		out[rel] = data
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func checkModes(t *testing.T, root string) {
	t.Helper()
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		perm := info.Mode().Perm()
		if info.IsDir() {
			if perm != 0o700 {
				t.Fatalf("dir %s perm %o", path, perm)
			}
		} else if perm != 0o600 {
			t.Fatalf("file %s perm %o", path, perm)
		}
		if info.Mode()&os.ModeSymlink != 0 {
			t.Fatalf("symlink %s", path)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func simpleSession(t *testing.T, archiveID, logical, title string, created any, model any, text string) []map[string]any {
	t.Helper()
	body := map[string]any{"archive_id": archiveID, "title": title}
	if created != nil {
		body["created_at"] = created
	}
	if model != nil {
		body["model"] = model
	}
	sess := makeRecord(t, archiveID, "session", logical, testNS, body, nil)
	sid := sess["entity_id"].(string)
	msg := makeRecord(t, archiveID, "message", logical+":m0", testNS, map[string]any{
		"content":        []any{map[string]any{"text": text, "type": "text"}},
		"role":           "user",
		"session_id":     sid,
		"source_ordinal": int64(0),
		"visibility":     "user_visible",
	}, nil)
	return []map[string]any{sess, msg}
}

func TestBuildRendersWithoutOpeningSources(t *testing.T) {
	root := t.TempDir()
	recs := simpleSession(t, "arch-one", "chat", "Hello", "2026-01-02T00:00:00Z", map[string]any{"model_name": "m", "provider": "p"}, "hi")
	archive, att := writeArchive(t, root, "arch-one", "batch-0001", recs, true)
	out := filepath.Join(root, "vault")
	if err := Build(archive, att, out); err != nil {
		t.Fatal(err)
	}
	files := collectFiles(t, out)
	if _, ok := files["index.md"]; !ok {
		t.Fatal("missing index")
	}
	if _, ok := files[MarkerName]; !ok {
		t.Fatal("missing marker")
	}
	joined := ""
	var chatCount int
	for name, data := range files {
		joined += string(data)
		if strings.HasPrefix(name, "chats/") {
			chatCount++
		}
	}
	if chatCount != 1 {
		t.Fatalf("chats %d", chatCount)
	}
	if strings.Contains(joined, "SENTINEL-SOURCE-BYTES-DO-NOT-COPY") || strings.Contains(joined, "SENTINEL-BLOB-BYTES-DO-NOT-COPY") {
		t.Fatal("copied source or blob bytes")
	}
	checkModes(t, out)
	if string(files[MarkerName]) != MarkerText {
		t.Fatalf("marker %q", files[MarkerName])
	}
}

func TestAttestationAndRecordTamperFailClosed(t *testing.T) {
	root := t.TempDir()
	recs := simpleSession(t, "arch-two", "chat", "T", "2026-01-01T00:00:00Z", nil, "x")
	archive, att := writeArchive(t, root, "arch-two", "batch-0001", recs, false)
	out := filepath.Join(root, "vault")
	if err := Build(archive, att, out); err != nil {
		t.Fatal(err)
	}
	first := collectFiles(t, out)

	ndjson := filepath.Join(archive, "batches", "batch-0001", "records.ndjson")
	raw, err := os.ReadFile(ndjson)
	if err != nil {
		t.Fatal(err)
	}
	raw[len(raw)-2] ^= 0x01
	if err := os.WriteFile(ndjson, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(ndjson, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := Build(archive, att, out); err == nil || ErrorCode(err) != AttestationHash {
		t.Fatalf("record tamper: %v", err)
	}
	after := collectFiles(t, out)
	if len(after) != len(first) {
		t.Fatal("output changed after failed tamper")
	}
	for k, v := range first {
		if !bytes.Equal(v, after[k]) {
			t.Fatalf("changed %s", k)
		}
	}

	out2 := filepath.Join(root, "vault-missing")
	attRaw, err := os.ReadFile(att)
	if err != nil {
		t.Fatal(err)
	}
	idx := bytes.Index(attRaw, []byte("sha256:"))
	if idx < 0 {
		t.Fatal("no digest")
	}
	tampered := append([]byte{}, attRaw...)
	tampered[idx+10] ^= 1
	att2 := filepath.Join(root, "attestation-bad.json")
	if err := os.WriteFile(att2, tampered, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(att2, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := Build(archive, att2, out2); err == nil {
		t.Fatal("expected attestation failure")
	}
	if _, err := os.Lstat(out2); !os.IsNotExist(err) {
		t.Fatal("installed output after attestation tamper")
	}
}

func TestHostileLiteralsStayLiteral(t *testing.T) {
	root := t.TempDir()
	hostile := "---\n!!python/object\n[[Index]]\n<img src=x onerror=alert(1)>\n" + strings.Repeat("`", 8) + " ticks"
	recs := simpleSession(t, "arch-hostile", "chat", hostile, nil, nil, hostile)
	archive, att := writeArchive(t, root, "arch-hostile", "batch-0001", recs, false)
	out := filepath.Join(root, "vault")
	if err := Build(archive, att, out); err != nil {
		t.Fatal(err)
	}
	files := collectFiles(t, out)
	if len(files) != 3 { // index, marker, one chat
		t.Fatalf("files %#v", keysOf(files))
	}
	var chat []byte
	for name, data := range files {
		if strings.HasPrefix(name, "chats/") {
			chat = data
			if strings.Contains(name, "Index") || strings.Contains(name, "hostile") {
				t.Fatalf("title leaked into filename %s", name)
			}
		}
	}
	text := string(chat)
	if !strings.HasPrefix(text, "---\n{") {
		t.Fatalf("frontmatter not json: %s", text[:40])
	}
	end := strings.Index(text[4:], "\n---\n")
	if end < 0 {
		t.Fatal("missing closing frontmatter")
	}
	fm := text[4 : 4+end]
	if strings.Contains(fm, "!!python") && !strings.Contains(fm, `!!python`) {
		t.Fatal("yaml tag")
	}
	if strings.Contains(fm, "\n!!") {
		t.Fatal("unquoted yaml tag line in json object")
	}
	if !strings.Contains(fm, `\n`) && strings.Contains(hostile, "\n") {
		// title newlines must be json-escaped in compact object
		if strings.Contains(fm, "\n---") {
			t.Fatal("raw title newline split frontmatter")
		}
	}
	body := text[4+end+5:]
	if !strings.Contains(body, hostile) && !strings.Contains(body, "[[Index]]") {
		t.Fatal("hostile text missing from fenced body")
	}
	if strings.Contains(body, "\n[[Index]]\n") && !strings.Contains(body, "```") && !strings.Contains(body, "````") {
		t.Fatal("wikilink escaped into structure")
	}
	index := string(files["index.md"])
	if !strings.Contains(index, "[[chat-") {
		t.Fatal("missing digest wikilink")
	}
	for _, line := range strings.Split(index, "\n") {
		trim := strings.TrimSpace(line)
		if strings.HasPrefix(trim, "- [[") && !strings.HasPrefix(trim, "- [[chat-") {
			t.Fatalf("wikilink target used title %q", trim)
		}
	}
}

func keysOf(m map[string][]byte) []string {
	var k []string
	for n := range m {
		k = append(k, n)
	}
	return k
}

func TestDuplicateTitlesDistinctFiles(t *testing.T) {
	root := t.TempDir()
	a := simpleSession(t, "arch-dup", "chat-a", "Same Title", "2026-01-01T00:00:00Z", nil, "a")
	b := simpleSession(t, "arch-dup", "chat-b", "Same Title", "2026-01-02T00:00:00Z", nil, "b")
	recs := append(a, b...)
	archive, att := writeArchive(t, root, "arch-dup", "batch-0001", recs, false)
	out := filepath.Join(root, "vault")
	if err := Build(archive, att, out); err != nil {
		t.Fatal(err)
	}
	files := collectFiles(t, out)
	var chats []string
	for name := range files {
		if strings.HasPrefix(name, "chats/") {
			chats = append(chats, name)
		}
	}
	if len(chats) != 2 {
		t.Fatalf("chats %v", chats)
	}
	if chats[0] == chats[1] {
		t.Fatal("same filename")
	}
	index := string(files["index.md"])
	if strings.Count(index, "[[chat-") != 2 {
		t.Fatalf("index links %s", index)
	}
}

func TestUnknownModelTimeAndSourceOrdinal(t *testing.T) {
	root := t.TempDir()
	recs := simpleSession(t, "arch-unk", "chat", "NoMeta", nil, nil, "hello")
	archive, att := writeArchive(t, root, "arch-unk", "batch-0001", recs, false)
	out := filepath.Join(root, "vault")
	if err := Build(archive, att, out); err != nil {
		t.Fatal(err)
	}
	var chat []byte
	for name, data := range collectFiles(t, out) {
		if strings.HasPrefix(name, "chats/") {
			chat = data
		}
	}
	text := string(chat)
	if !strings.Contains(text, `"model":null`) || !strings.Contains(text, `"created_at":null`) || !strings.Contains(text, `"updated_at":null`) {
		snip := text
		if len(snip) > 400 {
			snip = snip[:400]
		}
		t.Fatalf("nulls missing: %s", snip)
	}
	if !strings.Contains(text, "source order") || !strings.Contains(text, "not chronology") {
		t.Fatal("source_ordinal not labeled as source order")
	}
	if strings.Contains(text, "created_at: 0") {
		t.Fatal("used ordinal as time")
	}
}

func TestForestAndAmbiguousGraph(t *testing.T) {
	t.Run("forest", func(t *testing.T) {
		root := t.TempDir()
		archiveID := "arch-forest"
		var recs []map[string]any
		tmpMsgs := make([]map[string]any, 0, 27)
		for i := 0; i < 27; i++ {
			node := "node-" + itoa(i)
			tmpMsgs = append(tmpMsgs, makeRecord(t, archiveID, "message", "chat:"+node, owuiNS, map[string]any{
				"content":        []any{map[string]any{"text": node, "type": "text"}},
				"role":           "assistant",
				"session_id":     "pending",
				"source_ordinal": int64(i),
				"visibility":     "user_visible",
			}, map[string]any{
				owuiNS: map[string]any{"childrenIds": []any{}, "parentId": nil},
			}))
		}
		active := tmpMsgs[13]["entity_id"].(string)
		sess := makeRecord(t, archiveID, "session", "chat", owuiNS, map[string]any{
			"active_message_id": active,
			"archive_id":        archiveID,
			"thread_shape":      "unknown",
			"title":             "Synthetic forest",
		}, map[string]any{owuiNS: map[string]any{"thread_shape": "forest"}})
		sid := sess["entity_id"].(string)
		recs = append(recs, sess)
		for _, m := range tmpMsgs {
			m["body"].(map[string]any)["session_id"] = sid
			rid, err := RevisionID(m)
			if err != nil {
				t.Fatal(err)
			}
			m["revision_id"] = rid
			recs = append(recs, m)
		}
		archive, att := writeArchive(t, root, archiveID, "batch-0001", recs, false)
		out := filepath.Join(root, "vault")
		if err := Build(archive, att, out); err != nil {
			t.Fatal(err)
		}
		var page string
		for name, data := range collectFiles(t, out) {
			if strings.HasPrefix(name, "chats/") && strings.Contains(string(data), "Thread A") {
				page = string(data)
			}
		}
		if page == "" {
			t.Fatal("no forest page")
		}
		if !strings.Contains(page, "Thread A") || !strings.Contains(page, "Thread AA") {
			t.Fatal("missing thread labels")
		}
		if strings.Count(page, "contains active node: yes") != 1 {
			t.Fatalf("active count %d", strings.Count(page, "contains active node: yes"))
		}
		if !strings.Contains(page, `"derived_shape":"forest"`) || !strings.Contains(page, `"topology_basis":"complete_parent_child"`) {
			t.Fatal("forest not confirmed in frontmatter")
		}
		if !strings.Contains(page, `"declared_thread_shape":"unknown"`) {
			t.Fatal("declared shape should remain unknown")
		}
		for i := 0; i < 27; i++ {
			if !strings.Contains(page, "node-"+itoa(i)) {
				t.Fatalf("missing node %d", i)
			}
		}
	})
	t.Run("ambiguous", func(t *testing.T) {
		root := t.TempDir()
		archiveID := "arch-amb"
		sess := makeRecord(t, archiveID, "session", "chat", testNS, map[string]any{
			"archive_id": archiveID,
			"title":      "Ambiguous",
		}, nil)
		sid := sess["entity_id"].(string)
		m1 := makeRecord(t, archiveID, "message", "same", testNS, map[string]any{
			"content":        []any{map[string]any{"text": "rev-one", "type": "text"}},
			"role":           "assistant",
			"session_id":     sid,
			"source_ordinal": int64(0),
			"visibility":     "user_visible",
		}, nil)
		m2 := makeRecord(t, archiveID, "message", "same", testNS, map[string]any{
			"content":        []any{map[string]any{"text": "rev-two", "type": "text"}},
			"role":           "assistant",
			"session_id":     sid,
			"source_ordinal": int64(1),
			"visibility":     "user_visible",
		}, nil)
		other := makeRecord(t, archiveID, "message", "other", testNS, map[string]any{
			"content":        []any{map[string]any{"text": "other", "type": "text"}},
			"role":           "user",
			"session_id":     sid,
			"source_ordinal": int64(2),
			"visibility":     "user_visible",
		}, nil)
		if m1["entity_id"] != m2["entity_id"] {
			t.Fatal("expected same entity")
		}
		if m1["revision_id"] == m2["revision_id"] {
			t.Fatal("expected distinct revisions")
		}
		rel := makeRecord(t, archiveID, "relationship", "edge", testNS, map[string]any{
			"from_id": m1["entity_id"],
			"kind":    "parent_child",
			"to_id":   other["entity_id"],
		}, nil)
		archive, att := writeArchive(t, root, archiveID, "batch-0001", []map[string]any{sess, m1, m2, other, rel}, false)
		out := filepath.Join(root, "vault")
		if err := Build(archive, att, out); err != nil {
			t.Fatal(err)
		}
		var page string
		for name, data := range collectFiles(t, out) {
			if strings.HasPrefix(name, "chats/") {
				page = string(data)
			}
		}
		if !strings.Contains(page, "Graph rendering stopped") {
			t.Fatal("missing ambiguous honesty")
		}
		if !strings.Contains(page, "rev-one") || !strings.Contains(page, "rev-two") || !strings.Contains(page, "other") {
			t.Fatal("dropped revision cards")
		}
		if !strings.Contains(page, `"topology_basis":"revision_ambiguous"`) {
			t.Fatal("basis")
		}
	})
	t.Run("active-children", func(t *testing.T) {
		root := t.TempDir()
		archiveID := "arch-tree"
		p := makeRecord(t, archiveID, "message", "chat:p", owuiNS, map[string]any{
			"content":        []any{map[string]any{"text": "parent", "type": "text"}},
			"role":           "user",
			"session_id":     "pending",
			"source_ordinal": int64(0),
			"visibility":     "user_visible",
		}, map[string]any{owuiNS: map[string]any{"childrenIds": []any{"c1", "c2"}, "parentId": nil}})
		c1 := makeRecord(t, archiveID, "message", "chat:c1", owuiNS, map[string]any{
			"content":        []any{map[string]any{"text": "child-one", "type": "text"}},
			"role":           "assistant",
			"session_id":     "pending",
			"source_ordinal": int64(1),
			"visibility":     "user_visible",
		}, map[string]any{owuiNS: map[string]any{"childrenIds": []any{}, "parentId": "p"}})
		c2 := makeRecord(t, archiveID, "message", "chat:c2", owuiNS, map[string]any{
			"content":        []any{map[string]any{"text": "child-two", "type": "text"}},
			"role":           "assistant",
			"session_id":     "pending",
			"source_ordinal": int64(2),
			"visibility":     "user_visible",
		}, map[string]any{owuiNS: map[string]any{"childrenIds": []any{}, "parentId": "p"}})
		sess := makeRecord(t, archiveID, "session", "chat", owuiNS, map[string]any{
			"active_message_id": p["entity_id"],
			"archive_id":        archiveID,
			"thread_shape":      "tree",
			"title":             "Tree",
		}, map[string]any{owuiNS: map[string]any{"thread_shape": "tree"}})
		sid := sess["entity_id"].(string)
		for _, m := range []*map[string]any{&p, &c1, &c2} {
			(*m)["body"].(map[string]any)["session_id"] = sid
			rid, err := RevisionID(*m)
			if err != nil {
				t.Fatal(err)
			}
			(*m)["revision_id"] = rid
		}
		e1 := makeRecord(t, archiveID, "relationship", "chat:p:c1", owuiNS, map[string]any{
			"from_id":       p["entity_id"],
			"kind":          "parent_child",
			"sibling_index": int64(0),
			"to_id":         c1["entity_id"],
		}, nil)
		e2 := makeRecord(t, archiveID, "relationship", "chat:p:c2", owuiNS, map[string]any{
			"from_id":       p["entity_id"],
			"kind":          "parent_child",
			"sibling_index": int64(1),
			"to_id":         c2["entity_id"],
		}, nil)
		archive, att := writeArchive(t, root, archiveID, "batch-0001", []map[string]any{sess, p, c1, c2, e1, e2}, false)
		out := filepath.Join(root, "vault")
		if err := Build(archive, att, out); err != nil {
			t.Fatal(err)
		}
		var page string
		for name, data := range collectFiles(t, out) {
			if strings.HasPrefix(name, "chats/") {
				page = string(data)
			}
		}
		if !strings.Contains(page, `"derived_shape":"tree"`) {
			t.Fatalf("not tree: %s", page[strings.Index(page, "derived_shape"):strings.Index(page, "derived_shape")+40])
		}
		if !strings.Contains(page, "[active node]") {
			t.Fatal("active unmarked")
		}
		if !strings.Contains(page, "child-one") || !strings.Contains(page, "child-two") {
			t.Fatal("dropped branches")
		}
	})
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var b [12]byte
	n := len(b)
	for i > 0 {
		n--
		b[n] = byte('0' + i%10)
		i /= 10
	}
	return string(b[n:])
}

func TestRerunByteIdentical(t *testing.T) {
	root := t.TempDir()
	recs := simpleSession(t, "arch-rerun", "chat", "Once", "2026-03-01T00:00:00Z", nil, "x")
	archive, att := writeArchive(t, root, "arch-rerun", "batch-0001", recs, false)
	out := filepath.Join(root, "vault")
	if err := Build(archive, att, out); err != nil {
		t.Fatal(err)
	}
	first := collectFiles(t, out)
	if err := Build(archive, att, out); err != nil {
		t.Fatal(err)
	}
	second := collectFiles(t, out)
	if len(first) != len(second) {
		t.Fatalf("file count %d %d", len(first), len(second))
	}
	for k, v := range first {
		if !bytes.Equal(v, second[k]) {
			t.Fatalf("not identical %s", k)
		}
	}
	var chats int
	for name := range second {
		if strings.HasPrefix(name, "chats/") {
			chats++
		}
	}
	if chats != 1 {
		t.Fatalf("duplicates %d", chats)
	}
}

func TestRefuseUnownedOverlapSymlink(t *testing.T) {
	root := t.TempDir()
	recs := simpleSession(t, "arch-ref", "chat", "X", nil, nil, "x")
	archive, att := writeArchive(t, root, "arch-ref", "batch-0001", recs, false)

	unowned := filepath.Join(root, "unowned")
	if err := os.Mkdir(unowned, 0o700); err != nil {
		t.Fatal(err)
	}
	note := filepath.Join(unowned, "notes.md")
	if err := os.WriteFile(note, []byte("keep"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := Build(archive, att, unowned); err == nil || ErrorCode(err) != RefuseUnownedOutput {
		t.Fatalf("unowned: %v", err)
	}
	if raw, _ := os.ReadFile(note); string(raw) != "keep" {
		t.Fatal("deleted unowned note")
	}

	inside := filepath.Join(archive, "generated")
	if err := Build(archive, att, inside); err == nil || ErrorCode(err) != RefuseInputOutputOverlap {
		t.Fatalf("overlap: %v", err)
	}

	target := filepath.Join(root, "target")
	if err := os.Mkdir(target, 0o700); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(root, "link-out")
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	if err := Build(archive, att, link); err == nil || ErrorCode(err) != RefuseSymlink {
		t.Fatalf("symlink out: %v", err)
	}

	archLink := filepath.Join(root, "arch-link")
	if err := os.Symlink(archive, archLink); err != nil {
		t.Fatal(err)
	}
	if err := Build(archLink, att, filepath.Join(root, "from-link")); err == nil || ErrorCode(err) != RefuseSymlink {
		t.Fatalf("symlink archive: %v", err)
	}
}

func TestSiblingUserNoteSurvives(t *testing.T) {
	root := t.TempDir()
	recs := simpleSession(t, "arch-note", "chat", "N", "2026-01-01T00:00:00Z", nil, "n")
	archive, att := writeArchive(t, root, "arch-note", "batch-0001", recs, false)
	parent := filepath.Join(root, "obsidian")
	if err := os.Mkdir(parent, 0o700); err != nil {
		t.Fatal(err)
	}
	note := filepath.Join(parent, "user-note.md")
	if err := os.WriteFile(note, []byte("operator note"), 0o600); err != nil {
		t.Fatal(err)
	}
	out := filepath.Join(parent, "vault")
	if err := Build(archive, att, out); err != nil {
		t.Fatal(err)
	}
	if err := Build(archive, att, out); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(note)
	if err != nil || string(raw) != "operator note" {
		t.Fatalf("note %s %v", raw, err)
	}
	checkModes(t, out)
}

func TestCLIVersionAndPrivateErrors(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := Main([]string{"--version"}, &stdout, &stderr)
	if code != 0 || stderr.Len() != 0 {
		t.Fatalf("version %d %s %s", code, stdout.String(), stderr.String())
	}
	if !strings.Contains(stdout.String(), `"generated_by":"oca-vault"`) || !strings.Contains(stdout.String(), Version) {
		t.Fatalf("version body %s", stdout.String())
	}
	stdout.Reset()
	stderr.Reset()
	secret := filepath.Join(t.TempDir(), "secret-path-should-not-leak")
	code = Main([]string{"build", "--archive", secret, "--attestation", secret + "-att", "--output", secret + "-out"}, &stdout, &stderr)
	if code == 0 {
		t.Fatal("expected failure")
	}
	errText := stderr.String()
	if strings.Contains(errText, secret) || strings.Contains(errText, "secret-path") {
		t.Fatalf("leaked path %s", errText)
	}
	if !strings.HasPrefix(errText, `{"error":"`) || !strings.HasSuffix(errText, "\"}\n") {
		t.Fatalf("stderr %q", errText)
	}
	stdout.Reset()
	stderr.Reset()
	code = Main([]string{"build", "--archive", "rel", "--attestation", "rel2", "--output", "rel3"}, &stdout, &stderr)
	if code != 2 || ErrorCode(errc(ArgumentInvalid, 2)) != ArgumentInvalid {
		t.Fatalf("rel %d %s", code, stderr.String())
	}
	if strings.Contains(stderr.String(), "rel") && strings.Contains(stderr.String(), "error") {
		if stderr.String() != "{\"error\":\"ARGUMENT_INVALID\"}\n" {
			t.Fatalf("stderr %q", stderr.String())
		}
	}
}

func TestCLIBuild(t *testing.T) {
	root := t.TempDir()
	recs := simpleSession(t, "arch-cli", "chat", "CLI", "2026-01-01T00:00:00Z", nil, "z")
	archive, att := writeArchive(t, root, "arch-cli", "batch-0001", recs, false)
	out := filepath.Join(root, "vault")
	var stdout, stderr bytes.Buffer
	code := Main([]string{"build", "--archive", archive, "--attestation", att, "--output", out}, &stdout, &stderr)
	if code != 0 {
		t.Fatalf("%d %s %s", code, stdout.String(), stderr.String())
	}
	if _, err := os.Stat(filepath.Join(out, "index.md")); err != nil {
		t.Fatal(err)
	}
}
