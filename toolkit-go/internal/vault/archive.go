package vault

import (
	"path/filepath"
	"sort"
	"strings"
)

type RecordKey struct {
	Entity   string
	Revision string
}

func (k RecordKey) Less(o RecordKey) bool {
	if k.Entity != o.Entity {
		return k.Entity < o.Entity
	}
	return k.Revision < o.Revision
}

type Record struct {
	Raw        map[string]any
	Type       string
	Entity     string
	Revision   string
	Identity   map[string]any
	Body       map[string]any
	Extensions map[string]any
}

func (r Record) Key() RecordKey { return RecordKey{r.Entity, r.Revision} }

type Archive struct {
	ID      string
	Records map[RecordKey]Record
	Order   []RecordKey
}

func loadArchive(archiveRoot string, att *Attestation, lim Limits) (*Archive, error) {
	fi, err := lstatNoFollow(archiveRoot)
	if err != nil {
		return nil, err
	}
	if err := checkOwnerOnlyDir(fi); err != nil {
		return nil, err
	}
	base := filepath.Base(archiveRoot)
	if base != att.ArchiveID || !opaqueRe.MatchString(base) {
		return nil, errc(ArchiveIDMismatch, 1)
	}
	if err := reconcileBatches(archiveRoot, att, lim); err != nil {
		return nil, err
	}
	var total uint64
	records := map[RecordKey]Record{}
	order := []RecordKey{}
	payloads := map[RecordKey]string{}
	for _, b := range att.Batches {
		manPath, err := joinUnder(archiveRoot, b.Manifest.Path, lim)
		if err != nil {
			return nil, err
		}
		manRaw, err := openRegularNoFollow(manPath, lim.MaxFileBytes)
		if err != nil {
			return nil, err
		}
		total += uint64(len(manRaw))
		if total > lim.MaxTotalBytes {
			return nil, errc(LimitExceeded, 1)
		}
		if int64(len(manRaw)) != b.Manifest.Size || sha256Digest(manRaw) != b.Manifest.ByteDigest {
			return nil, errc(AttestationHash, 1)
		}
		manVal, err := ParseJSON(manRaw, lim)
		if err != nil {
			return nil, err
		}
		manObj, ok := asObject(manVal)
		if !ok {
			return nil, errc(RecordInvalid, 1)
		}
		manArchive, _ := asString(manObj["archive_id"])
		manBatch, _ := asString(manObj["batch_id"])
		if manArchive != att.ArchiveID || manBatch != b.BatchID {
			return nil, errc(ArchiveIDMismatch, 1)
		}

		recPath, err := joinUnder(archiveRoot, b.Records.Path, lim)
		if err != nil {
			return nil, err
		}
		recRaw, err := openRegularNoFollow(recPath, lim.MaxFileBytes)
		if err != nil {
			return nil, err
		}
		total += uint64(len(recRaw))
		if total > lim.MaxTotalBytes {
			return nil, errc(LimitExceeded, 1)
		}
		if int64(len(recRaw)) != b.Records.Size || sha256Digest(recRaw) != b.Records.ByteDigest {
			return nil, errc(AttestationHash, 1)
		}
		items, err := ParseNDJSON(recRaw, lim)
		if err != nil {
			return nil, err
		}
		if int64(len(items)) != b.Records.Count {
			return nil, errc(AttestationMismatch, 1)
		}
		for _, item := range items {
			rec, err := parseRecord(item, att.ArchiveID)
			if err != nil {
				return nil, err
			}
			key := rec.Key()
			canon, err := CanonicalBytes(rec.Raw)
			if err != nil {
				return nil, err
			}
			if prior, ok := payloads[key]; ok {
				if prior != string(canon) || records[key].Type != rec.Type {
					return nil, errc(RecordInvalid, 1)
				}
				continue
			}
			payloads[key] = string(canon)
			records[key] = rec
			order = append(order, key)
		}
	}
	return &Archive{ID: att.ArchiveID, Records: records, Order: order}, nil
}

func parseRecord(v any, archiveID string) (Record, error) {
	obj, ok := asObject(v)
	if !ok {
		return Record{}, errc(RecordInvalid, 1)
	}
	rt, ok := asString(obj["record_type"])
	if !ok || rt == "" {
		return Record{}, errc(RecordInvalid, 1)
	}
	entity, ok := asString(obj["entity_id"])
	if !ok || !digestRe.MatchString(entity) {
		return Record{}, errc(RecordInvalid, 1)
	}
	rev, ok := asString(obj["revision_id"])
	if !ok || !digestRe.MatchString(rev) {
		return Record{}, errc(RecordInvalid, 1)
	}
	ident, ok := asObject(obj["identity"])
	if !ok {
		return Record{}, errc(RecordInvalid, 1)
	}
	body, ok := asObject(obj["body"])
	if !ok {
		return Record{}, errc(RecordInvalid, 1)
	}
	identArchive, _ := asString(ident["archive_id"])
	if identArchive != archiveID {
		return Record{}, errc(ArchiveIDMismatch, 1)
	}
	wantEntity, err := EntityID(ident)
	if err != nil {
		return Record{}, err
	}
	if wantEntity != entity {
		return Record{}, errc(EntityIDMismatch, 1)
	}
	wantRev, err := RevisionID(obj)
	if err != nil {
		return Record{}, err
	}
	if wantRev != rev {
		return Record{}, errc(RevisionIDMismatch, 1)
	}
	ext, _ := asObject(obj["extensions"])
	if ext == nil {
		if rawExt, exists := obj["extensions"]; exists && rawExt != nil {
			return Record{}, errc(RecordInvalid, 1)
		}
		ext = map[string]any{}
	}
	return Record{
		Raw:        obj,
		Type:       rt,
		Entity:     entity,
		Revision:   rev,
		Identity:   ident,
		Body:       body,
		Extensions: ext,
	}, nil
}

func digestHex(id string) (string, bool) {
	if !strings.HasPrefix(id, "sha256:") || len(id) != 7+64 {
		return "", false
	}
	h := id[7:]
	for i := 0; i < len(h); i++ {
		c := h[i]
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			return "", false
		}
	}
	return h, true
}

func pageFileName(key RecordKey) (string, error) {
	eh, ok := digestHex(key.Entity)
	if !ok {
		return "", errc(RecordInvalid, 1)
	}
	rh, ok := digestHex(key.Revision)
	if !ok {
		return "", errc(RecordInvalid, 1)
	}
	return "chat-" + eh + "-" + rh + ".md", nil
}

func wikilinkTarget(key RecordKey) (string, error) {
	name, err := pageFileName(key)
	if err != nil {
		return "", err
	}
	return strings.TrimSuffix(name, ".md"), nil
}

func sortedKeys(keys []RecordKey) []RecordKey {
	out := append([]RecordKey(nil), keys...)
	sort.Slice(out, func(i, j int) bool { return out[i].Less(out[j]) })
	return out
}
