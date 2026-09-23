package vault

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPythonAttestationContractFixture(t *testing.T) {
	raw, err := os.ReadFile("testdata/attestation-aligned.json")
	if err != nil {
		t.Fatal(err)
	}
	lim := DefaultLimits()
	att, err := parseAttestation(raw, lim)
	if err != nil {
		t.Fatal(err)
	}
	if att.ArchiveID != "fixture-arc" || att.ValidatorVersion != "0.1.0.dev0" {
		t.Fatalf("archive/validator %+v", att)
	}
	if len(att.Batches) != 1 || att.Batches[0].BatchID != "batch-0001" {
		t.Fatalf("batches %+v", att.Batches)
	}
	man, err := os.ReadFile("testdata/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	rec, err := os.ReadFile("testdata/records.ndjson")
	if err != nil {
		t.Fatal(err)
	}
	if sha256Digest(man) != att.Batches[0].Manifest.ByteDigest {
		t.Fatalf("manifest hash %s", att.Batches[0].Manifest.ByteDigest)
	}
	if sha256Digest(rec) != att.Batches[0].Records.ByteDigest {
		t.Fatalf("records hash %s", att.Batches[0].Records.ByteDigest)
	}
	if int64(len(man)) != att.Batches[0].Manifest.Size || int64(len(rec)) != att.Batches[0].Records.Size {
		t.Fatal("size mismatch")
	}
	if att.Batches[0].Records.Count != 1 || att.Batches[0].Manifest.Count != -1 {
		t.Fatal("count")
	}
	parsed, err := ParseJSON(raw, lim)
	if err != nil {
		t.Fatal(err)
	}
	canon, err := CanonicalBytes(parsed)
	if err != nil {
		t.Fatal(err)
	}
	want := bytes.TrimSuffix(raw, []byte{'\n'})
	if !bytes.Equal(canon, want) {
		t.Fatalf("python canonical mismatch\n got %s\nwant %s", canon, want)
	}
	obj, _ := asObject(parsed)
	if err := exactKeys(obj, "archive_id", "attestation_kind", "batches", "format", "spec_version", "status", "validator"); err != nil {
		t.Fatal(err)
	}
	manObj, _ := asObject(obj["batches"].([]any)[0].(map[string]any)["manifest"])
	if err := exactKeys(manObj, "byte_digest", "path", "size"); err != nil {
		t.Fatal(err)
	}
	recObj, _ := asObject(obj["batches"].([]any)[0].(map[string]any)["records"])
	if err := exactKeys(recObj, "byte_digest", "count", "path", "size"); err != nil {
		t.Fatal(err)
	}
}

func TestAttestationExactKeysRejectAliases(t *testing.T) {
	raw, err := os.ReadFile("testdata/attestation-aligned.json")
	if err != nil {
		t.Fatal(err)
	}
	v, err := ParseJSON(raw, DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	obj := v.(map[string]any)
	batch := obj["batches"].([]any)[0].(map[string]any)
	man := batch["manifest"].(map[string]any)
	man["sha256"] = man["byte_digest"]
	delete(man, "byte_digest")
	alias, err := CanonicalBytes(obj)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := parseAttestation(alias, DefaultLimits()); err == nil || ErrorCode(err) != AttestationInvalid {
		t.Fatalf("sha256 alias: %v", err)
	}
	v2, _ := ParseJSON(raw, DefaultLimits())
	obj2 := v2.(map[string]any)
	obj2["extra"] = true
	extra, _ := CanonicalBytes(obj2)
	if _, err := parseAttestation(extra, DefaultLimits()); err == nil || ErrorCode(err) != AttestationInvalid {
		t.Fatalf("extra key: %v", err)
	}
	v3, _ := ParseJSON(raw, DefaultLimits())
	obj3 := v3.(map[string]any)
	delete(obj3, "attestation_kind")
	delete(obj3, "spec_version")
	missing, _ := CanonicalBytes(obj3)
	if _, err := parseAttestation(missing, DefaultLimits()); err == nil || ErrorCode(err) != AttestationInvalid {
		t.Fatalf("missing kind/spec: %v", err)
	}
}

func TestBigIntIdentityParity(t *testing.T) {
	raw := []byte(`{"logical_key":9223372036854775808,"n":18446744073709551616,"neg":-9223372036854775809}`)
	v, err := ParseJSON(raw, DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	canon, err := CanonicalBytes(v)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(canon, raw) {
		t.Fatalf("got %s", canon)
	}
	d, err := Digest(v)
	if err != nil {
		t.Fatal(err)
	}
	const pythonDigest = "sha256:fa546e14f6accb75f2bbdb0d94aa2215200286a3b2dbf8954ce4b78a02bbb76f"
	if d != pythonDigest {
		t.Fatalf("digest %s want %s", d, pythonDigest)
	}
	recRaw := []byte(`{"body":{"archive_id":"arch-big","extra":18446744073709551616,"source_ordinal":9223372036854775808,"title":"t"},"entity_id":"sha256:9cb23398b6aee2bb10bb9628c6dd0b062980439d332c46092f6537ce591ddd15","identity":{"adapter_namespace":"org.example","archive_id":"arch-big","identity_scheme":"native-id","identity_version":"1","logical_key":"k","record_type":"session","source_scope_id":"scope"},"record_type":"session","sensitivity":"restricted-unclassified","spec_version":"0.1"}`)
	recVal, err := ParseJSON(recRaw, DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	rec := recVal.(map[string]any)
	gotCanon, err := CanonicalBytes(rec)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(gotCanon, recRaw) {
		t.Fatalf("record canonical %s", gotCanon)
	}
	eid, err := EntityID(rec["identity"].(map[string]any))
	if err != nil || eid != rec["entity_id"].(string) {
		t.Fatalf("entity %s %v", eid, err)
	}
	rid, err := RevisionID(rec)
	if err != nil {
		t.Fatal(err)
	}
	const pythonRev = "sha256:8ab3af2f443e52521a7afcb42e8607e1141e6ce2cb67522b0d4346389c30da3a"
	if rid != pythonRev {
		t.Fatalf("revision %s want %s", rid, pythonRev)
	}
	neg0, err := ParseJSON([]byte("-0"), DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	z, err := CanonicalBytes(neg0)
	if err != nil || string(z) != "0" {
		t.Fatalf("-0 canonical %s %v", z, err)
	}
}

func TestFrontmatterYAMLLineBreaks(t *testing.T) {
	title := "keep\u2028---\n# pwned\u2029\u0085end"
	rawIdent, err := CanonicalBytes(title)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(rawIdent, []byte{0xE2, 0x80, 0xA8}) || !bytes.Contains(rawIdent, []byte{0xE2, 0x80, 0xA9}) {
		t.Fatal("identity hashing must keep U+2028/U+2029")
	}
	if bytes.Contains(rawIdent, []byte(`\u2028`)) {
		t.Fatal("identity hashing must not escape U+2028")
	}
	fm, err := FrontmatterBytes(map[string]any{"title": title, "page_kind": "chat"})
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(fm, []byte{0xE2, 0x80, 0xA8}) || bytes.Contains(fm, []byte{0xE2, 0x80, 0xA9}) || bytes.Contains(fm, []byte{0xC2, 0x85}) {
		t.Fatal("frontmatter leaked raw line separators")
	}
	if !bytes.Contains(fm, []byte(`\u2028`)) || !bytes.Contains(fm, []byte(`\u2029`)) || !bytes.Contains(fm, []byte(`\u0085`)) {
		t.Fatalf("missing unicode escapes %s", fm)
	}
	root := t.TempDir()
	recs := simpleSession(t, "arch-yaml", "chat", title, "2026-01-01T00:00:00Z", nil, "hi")
	archive, att := writeArchive(t, root, "arch-yaml", "batch-0001", recs, false)
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
	if len(chat) == 0 {
		t.Fatal("missing chat")
	}
	text := string(chat)
	if !strings.HasPrefix(text, "---\n") {
		t.Fatal("missing opening fence")
	}
	rest := text[4:]
	end := strings.Index(rest, "\n---\n")
	if end < 0 {
		t.Fatal("missing closing fence")
	}
	fmLine := rest[:end]
	if strings.ContainsFunc(fmLine, func(r rune) bool {
		return r == '\n' || r == '\r' || r == '\u0085' || r == '\u2028' || r == '\u2029' || r == '\v' || r == '\f'
	}) {
		t.Fatal("frontmatter is not one physical line")
	}
	parsed, err := ParseJSON([]byte(fmLine), DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	gotTitle, _ := asString(parsed.(map[string]any)["title"])
	if gotTitle != title {
		t.Fatalf("title round-trip %q", gotTitle)
	}
	lines := strings.FieldsFunc(text, func(r rune) bool {
		return r == '\n' || r == '\r' || r == '\u0085' || r == '\u2028' || r == '\u2029'
	})
	if len(lines) < 3 || lines[0] != "---" {
		t.Fatalf("obsidian split %#v", lines)
	}
}

func TestCompetingHintsByteIdentical(t *testing.T) {
	buildHints := func(t *testing.T, archiveID string, extFor func(idA, idZ string) map[string]any) (string, map[string][]byte, string, string) {
		t.Helper()
		root := t.TempDir()
		fromA := makeRecord(t, archiveID, "message", "chat:from-a", owuiNS, map[string]any{
			"content":        []any{map[string]any{"text": "A", "type": "text"}},
			"role":           "user",
			"session_id":     "pending",
			"source_ordinal": int64(0),
			"visibility":     "user_visible",
		}, nil)
		fromZ := makeRecord(t, archiveID, "message", "chat:from-z", owuiNS, map[string]any{
			"content":        []any{map[string]any{"text": "Z", "type": "text"}},
			"role":           "assistant",
			"session_id":     "pending",
			"source_ordinal": int64(1),
			"visibility":     "user_visible",
		}, nil)
		idA, idZ := fromA["entity_id"].(string), fromZ["entity_id"].(string)
		sess := makeRecord(t, archiveID, "session", "chat", owuiNS, map[string]any{
			"archive_id": archiveID,
			"title":      "Hints",
		}, extFor(idA, idZ))
		sid := sess["entity_id"].(string)
		for _, m := range []*map[string]any{&fromA, &fromZ} {
			(*m)["body"].(map[string]any)["session_id"] = sid
			rid, err := RevisionID(*m)
			if err != nil {
				t.Fatal(err)
			}
			(*m)["revision_id"] = rid
		}
		e1 := makeRecord(t, archiveID, "relationship", "chat:a:z", owuiNS, map[string]any{
			"from_id":       fromA["entity_id"],
			"kind":          "parent_child",
			"sibling_index": int64(0),
			"to_id":         fromZ["entity_id"],
		}, nil)
		archive, att := writeArchive(t, root, archiveID, "batch-0001", []map[string]any{sess, fromA, fromZ, e1}, false)
		out := filepath.Join(root, "vault")
		if err := Build(archive, att, out); err != nil {
			t.Fatal(err)
		}
		files := collectFiles(t, out)
		if err := Build(archive, att, out); err != nil {
			t.Fatal(err)
		}
		second := collectFiles(t, out)
		if len(files) != len(second) {
			t.Fatalf("file count %d %d", len(files), len(second))
		}
		for k, v := range files {
			if !bytes.Equal(v, second[k]) {
				t.Fatalf("not identical %s", k)
			}
		}
		var page string
		for name, data := range files {
			if strings.HasPrefix(name, "chats/") {
				page = string(data)
			}
		}
		return page, files, fromA["entity_id"].(string), fromZ["entity_id"].(string)
	}

	t.Run("sorted-keys", func(t *testing.T) {
		page, _, idA, idZ := buildHints(t, "arch-hint-sort", func(idA, idZ string) map[string]any {
			return map[string]any{
				"z-ns": map[string]any{"currentId": "from-z"},
				"a-ns": map[string]any{"currentId": "from-a"},
			}
		})
		if !strings.Contains(page, idA+"` role user") || !strings.Contains(page, "[active node]") {
			t.Fatal("expected sorted-key winner from-a")
		}
		if strings.Count(page, "[active node]") != 1 {
			t.Fatalf("active count %d", strings.Count(page, "[active node]"))
		}
		if strings.Contains(page, idZ+"` role assistant source_ordinal 1 (source order, not time) [active node]") {
			t.Fatal("z-ns currentId won unsorted walk")
		}
	})
	t.Run("fixed-precedence", func(t *testing.T) {
		page, _, idA, idZ := buildHints(t, "arch-hint-prec", func(idA, idZ string) map[string]any {
			return map[string]any{
				"ns": map[string]any{
					"currentId":         "from-z",
					"active_message_id": idA,
				},
			}
		})
		if !strings.Contains(page, idA) || strings.Count(page, "[active node]") != 1 {
			t.Fatal("expected active_message_id precedence")
		}
		if strings.Contains(page, idZ+"` role assistant source_ordinal 1 (source order, not time) [active node]") {
			t.Fatal("currentId won over active_message_id")
		}
	})
}

func TestMaliciousForgedAttestation(t *testing.T) {
	root := t.TempDir()
	recs := simpleSession(t, "arch-forge", "chat", "T", "2026-01-01T00:00:00Z", nil, "x")
	recs[1]["body"].(map[string]any)["role"] = "user\n# pwned [active node]\n- injected"
	rid, err := RevisionID(recs[1])
	if err != nil {
		t.Fatal(err)
	}
	recs[1]["revision_id"] = rid
	archive, att := writeArchive(t, root, "arch-forge", "batch-0001", recs, false)
	out := filepath.Join(root, "vault")
	if err := Build(archive, att, out); err == nil || ErrorCode(err) != RecordInvalid {
		t.Fatalf("malicious role: %v", err)
	}
	if _, err := os.Lstat(out); !os.IsNotExist(err) {
		t.Fatal("installed output for forged role")
	}

	root2 := t.TempDir()
	recs2 := simpleSession(t, "arch-forge2", "chat", "T", "2026-01-01T00:00:00Z", nil, "x")
	recs2[1]["body"].(map[string]any)["content"] = []any{map[string]any{
		"text": "x",
		"type": "text\n#### injected",
	}}
	rid2, err := RevisionID(recs2[1])
	if err != nil {
		t.Fatal(err)
	}
	recs2[1]["revision_id"] = rid2
	archive2, att2 := writeArchive(t, root2, "arch-forge2", "batch-0001", recs2, false)
	out2 := filepath.Join(root2, "vault")
	if err := Build(archive2, att2, out2); err == nil || ErrorCode(err) != RecordInvalid {
		t.Fatalf("malicious content type: %v", err)
	}

	root3 := t.TempDir()
	good := simpleSession(t, "arch-forge3", "chat", "T", "2026-01-01T00:00:00Z", nil, "x")
	archive3, att3 := writeArchive(t, root3, "arch-forge3", "batch-0001", good, false)
	out3 := filepath.Join(root3, "vault")
	if err := Build(archive3, att3, out3); err != nil {
		t.Fatal(err)
	}
	attRaw, err := os.ReadFile(att3)
	if err != nil {
		t.Fatal(err)
	}
	v, err := ParseJSON(attRaw, DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	obj := v.(map[string]any)
	obj["note"] = "not-schema-validation"
	forged, err := CanonicalBytes(obj)
	if err != nil {
		t.Fatal(err)
	}
	forged = append(forged, '\n')
	attBad := filepath.Join(root3, "forged.json")
	if err := os.WriteFile(attBad, forged, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(attBad, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := Build(archive3, attBad, out3); err == nil || ErrorCode(err) != AttestationInvalid {
		t.Fatalf("forged extra key: %v", err)
	}
	if _, err := os.Stat(filepath.Join(out3, "index.md")); err != nil {
		t.Fatal("prior output must remain after forged attestation")
	}
}

func TestPlantedMarkerAndParentOwnership(t *testing.T) {
	root := t.TempDir()
	recs := simpleSession(t, "arch-plant", "chat", "X", nil, nil, "x")
	archive, att := writeArchive(t, root, "arch-plant", "batch-0001", recs, false)

	symlinkOut := filepath.Join(root, "plant-symlink")
	if err := os.Mkdir(symlinkOut, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(root, "missing-marker"), filepath.Join(symlinkOut, MarkerName)); err != nil {
		t.Fatal(err)
	}
	if err := Build(archive, att, symlinkOut); err == nil || ErrorCode(err) != RefuseSymlink {
		t.Fatalf("planted symlink marker: %v", err)
	}

	dirOut := filepath.Join(root, "plant-dir")
	if err := os.Mkdir(dirOut, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(dirOut, MarkerName), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := Build(archive, att, dirOut); err == nil || ErrorCode(err) != RefuseNotRegular {
		t.Fatalf("planted dir marker: %v", err)
	}

	owned := filepath.Join(root, "owned")
	if err := Build(archive, att, owned); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(owned, MarkerName)
	if err := os.Link(marker, filepath.Join(owned, "hardlink")); err != nil {
		t.Fatal(err)
	}
	if err := Build(archive, att, owned); err == nil || ErrorCode(err) != RefuseOwnerOnly {
		t.Fatalf("nlink marker: %v", err)
	}
	if _, err := os.Stat(filepath.Join(owned, "index.md")); err != nil {
		t.Fatal("output removed after planted hardlink")
	}

	realParent := filepath.Join(root, "real-parent")
	if err := os.Mkdir(realParent, 0o700); err != nil {
		t.Fatal(err)
	}
	linkParent := filepath.Join(root, "link-parent")
	if err := os.Symlink(realParent, linkParent); err != nil {
		t.Fatal(err)
	}
	if err := Build(archive, att, filepath.Join(linkParent, "vault")); err == nil || ErrorCode(err) != RefuseSymlink {
		t.Fatalf("symlink parent: %v", err)
	}
}

func TestRecoveryInjectionPreservesOutputAndNotes(t *testing.T) {
	t.Cleanup(resetTestHook)
	root := t.TempDir()
	recs := simpleSession(t, "arch-recov", "chat", "R", "2026-01-01T00:00:00Z", nil, "r")
	archive, att := writeArchive(t, root, "arch-recov", "batch-0001", recs, false)
	parent := filepath.Join(root, "notes")
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
	first := collectFiles(t, out)

	testHook.failRenameStaging = true
	testHook.failRestore = false
	if err := Build(archive, att, out); err == nil || ErrorCode(err) != OutputWrite {
		t.Fatalf("staging fail: %v", err)
	}
	after := collectFiles(t, out)
	if len(after) != len(first) {
		t.Fatal("output not restored")
	}
	for k, v := range first {
		if !bytes.Equal(v, after[k]) {
			t.Fatalf("changed %s", k)
		}
	}
	if raw, _ := os.ReadFile(note); string(raw) != "operator note" {
		t.Fatal("note lost on restore")
	}

	testHook.failRenameStaging = true
	testHook.failRestore = true
	if err := Build(archive, att, out); err == nil || ErrorCode(err) != OutputWrite {
		t.Fatalf("restore fail: %v", err)
	}
	if _, err := os.Lstat(out); !os.IsNotExist(err) {
		t.Fatal("output path should be absent when restore fails")
	}
	backup := findBackup(t, parent, "vault-old")
	restored := collectFiles(t, backup)
	if len(restored) != len(first) {
		t.Fatalf("backup files %d want %d", len(restored), len(first))
	}
	for k, v := range first {
		if !bytes.Equal(v, restored[k]) {
			t.Fatalf("backup changed %s", k)
		}
	}
	if raw, _ := os.ReadFile(note); string(raw) != "operator note" {
		t.Fatal("note lost when restore failed")
	}

	resetTestHook()
	if err := os.Rename(backup, out); err != nil {
		t.Fatal(err)
	}
	testHook.failDirSync = true
	if err := Build(archive, att, out); err == nil || ErrorCode(err) != OutputWrite {
		t.Fatalf("dir sync: %v", err)
	}
	still := collectFiles(t, out)
	for k, v := range first {
		if !bytes.Equal(v, still[k]) {
			t.Fatalf("dir sync mutated %s", k)
		}
	}
	if raw, _ := os.ReadFile(note); string(raw) != "operator note" {
		t.Fatal("note lost on dir sync fail")
	}
}

func findBackup(t *testing.T, parent, base string) string {
	t.Helper()
	prefix := "." + base + ".vault-"
	entries, err := os.ReadDir(parent)
	if err != nil {
		t.Fatal(err)
	}
	var found []string
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), prefix) {
			found = append(found, filepath.Join(parent, e.Name()))
		}
	}
	if len(found) != 1 {
		t.Fatalf("backups %v", found)
	}
	return found[0]
}
