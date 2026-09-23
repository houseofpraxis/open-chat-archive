package vault

import (
	"os"
	"path/filepath"
)

func prepareOutput(output string, inputs []string) (staging string, err error) {
	for _, in := range inputs {
		if overlaps(output, in) {
			return "", errc(RefuseInputOutputOverlap, 3)
		}
	}
	if fi, err := os.Lstat(output); err == nil {
		if fi.Mode()&os.ModeSymlink != 0 {
			return "", errc(RefuseSymlink, 3)
		}
		if !fi.IsDir() {
			return "", errc(RefuseUnownedOutput, 3)
		}
		if err := verifyReplaceableOutput(output); err != nil {
			return "", err
		}
	} else if !os.IsNotExist(err) {
		return "", errc(OutputWrite, 4)
	}
	parent := filepath.Dir(output)
	if err := ensureOutputParent(parent); err != nil {
		return "", err
	}
	base := filepath.Base(output)
	if base == "" || base == "." || base == ".." {
		return "", errc(ArgumentInvalid, 2)
	}
	staging, err = stagingName(parent, base)
	if err != nil {
		return "", err
	}
	if err := mkdir0700(staging); err != nil {
		return "", err
	}
	return staging, nil
}

func ensureOutputParent(parent string) error {
	fi, err := os.Lstat(parent)
	if err == nil {
		if fi.Mode()&os.ModeSymlink != 0 {
			return errc(RefuseSymlink, 3)
		}
		if !fi.IsDir() {
			return errc(RefuseNotRegular, 3)
		}
		uid, _, ok := ownerUID(fi)
		if !ok || uid != uint32(os.Geteuid()) {
			return errc(RefuseUnownedOutput, 3)
		}
		return nil
	}
	if !os.IsNotExist(err) {
		return errc(OutputWrite, 4)
	}
	return mkdirAll0700(parent)
}

func verifyReplaceableOutput(output string) error {
	fi, err := os.Lstat(output)
	if err != nil {
		if os.IsNotExist(err) {
			return errc(PathMissing, 1)
		}
		return errc(OutputWrite, 4)
	}
	if fi.Mode()&os.ModeSymlink != 0 {
		return errc(RefuseSymlink, 3)
	}
	if !fi.IsDir() {
		return errc(RefuseUnownedOutput, 3)
	}
	if err := checkOwnerOnlyDir(fi); err != nil {
		return err
	}
	return verifyOwnedMarker(filepath.Join(output, MarkerName))
}

func verifyOwnedMarker(path string) error {
	mfi, err := os.Lstat(path)
	if err != nil {
		return errc(RefuseUnownedOutput, 3)
	}
	if mfi.Mode()&os.ModeSymlink != 0 {
		return errc(RefuseSymlink, 3)
	}
	if !mfi.Mode().IsRegular() {
		return errc(RefuseNotRegular, 3)
	}
	if err := checkOwnerOnlyFile(mfi); err != nil {
		return err
	}
	if mfi.Size() != int64(len(MarkerText)) {
		return errc(RefuseUnownedOutput, 3)
	}
	data, err := openRegularNoFollow(path, uint64(len(MarkerText)))
	if err != nil {
		return err
	}
	if string(data) != MarkerText {
		return errc(RefuseUnownedOutput, 3)
	}
	return nil
}

func installOutput(staging, output string) error {
	if err := lockPermissions(staging); err != nil {
		return err
	}
	if err := durableSyncTree(staging); err != nil {
		return err
	}
	parent := filepath.Dir(output)
	if err := ensureOutputParent(parent); err != nil {
		return err
	}
	if fi, err := os.Lstat(output); err == nil {
		if fi.Mode()&os.ModeSymlink != 0 {
			return errc(RefuseSymlink, 3)
		}
		if err := verifyReplaceableOutput(output); err != nil {
			return err
		}
		backup, err := stagingName(parent, filepath.Base(output)+"-old")
		if err != nil {
			return err
		}
		if err := os.Rename(output, backup); err != nil {
			return errc(OutputWrite, 4)
		}
		if testHook.failRenameStaging || os.Rename(staging, output) != nil {
			if testHook.failRestore || os.Rename(backup, output) != nil {
				// Preserve backup; do not delete sibling notes or the backup tree.
				return errc(OutputWrite, 4)
			}
			return errc(OutputWrite, 4)
		}
		_ = fsyncPath(parent, true)
		if err := removeAllNoFollow(backup); err != nil {
			return err
		}
		return nil
	} else if !os.IsNotExist(err) {
		return errc(OutputWrite, 4)
	}
	if testHook.failRenameStaging || os.Rename(staging, output) != nil {
		return errc(OutputWrite, 4)
	}
	_ = fsyncPath(parent, true)
	return nil
}

func writeVaultTree(staging string, archiveID string, arch *Archive, sessions []Session, lim Limits) error {
	var total uint64
	index, err := renderIndex(archiveID, sessions)
	if err != nil {
		return err
	}
	total += uint64(len(index))
	if total > lim.MaxOutputBytes {
		return errc(LimitExceeded, 1)
	}
	if err := writeFile0600(filepath.Join(staging, "index.md"), index); err != nil {
		return err
	}
	chats := filepath.Join(staging, "chats")
	if err := mkdir0700(chats); err != nil {
		return err
	}
	seen := map[string]struct{}{}
	for _, s := range sessions {
		name, err := pageFileName(s.Key)
		if err != nil {
			return err
		}
		if _, dup := seen[name]; dup {
			return errc(RecordInvalid, 1)
		}
		seen[name] = struct{}{}
		body, err := renderChat(archiveID, arch, s)
		if err != nil {
			return err
		}
		total += uint64(len(body))
		if total > lim.MaxOutputBytes {
			return errc(LimitExceeded, 1)
		}
		if err := writeFile0600(filepath.Join(chats, name), body); err != nil {
			return err
		}
	}
	if err := writeFile0600(filepath.Join(staging, MarkerName), []byte(MarkerText)); err != nil {
		return err
	}
	return nil
}
