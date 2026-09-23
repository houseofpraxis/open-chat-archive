package vault

import (
	"crypto/rand"
	"encoding/hex"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

func requireAbs(p string) (string, error) {
	if p == "" || !filepath.IsAbs(p) {
		return "", errc(ArgumentInvalid, 2)
	}
	cleaned := filepath.Clean(p)
	if !filepath.IsAbs(cleaned) || cleaned == "/" || cleaned == "." {
		return "", errc(ArgumentInvalid, 2)
	}
	return cleaned, nil
}

func overlaps(a, b string) bool {
	a = filepath.Clean(a)
	b = filepath.Clean(b)
	if a == b {
		return true
	}
	sep := string(os.PathSeparator)
	return strings.HasPrefix(a, b+sep) || strings.HasPrefix(b, a+sep)
}

func lstatNoFollow(path string) (os.FileInfo, error) {
	fi, err := os.Lstat(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, errc(PathMissing, 1)
		}
		return nil, errc(PathMissing, 1)
	}
	if fi.Mode()&os.ModeSymlink != 0 {
		return nil, errc(RefuseSymlink, 3)
	}
	return fi, nil
}

func ownerUID(fi os.FileInfo) (uint32, uint64, bool) {
	st, ok := fi.Sys().(*syscall.Stat_t)
	if !ok {
		return 0, 0, false
	}
	return st.Uid, uint64(st.Nlink), true
}

func checkOwnerOnlyFile(fi os.FileInfo) error {
	if fi.Mode()&os.ModeSymlink != 0 {
		return errc(RefuseSymlink, 3)
	}
	if !fi.Mode().IsRegular() {
		return errc(RefuseNotRegular, 3)
	}
	if fi.Mode().Perm()&0o077 != 0 {
		return errc(RefuseOwnerOnly, 3)
	}
	uid, nlink, ok := ownerUID(fi)
	if !ok || uid != uint32(os.Geteuid()) || nlink != 1 {
		return errc(RefuseOwnerOnly, 3)
	}
	return nil
}

func checkOwnerOnlyDir(fi os.FileInfo) error {
	if fi.Mode()&os.ModeSymlink != 0 {
		return errc(RefuseSymlink, 3)
	}
	if !fi.IsDir() {
		return errc(RefuseNotRegular, 3)
	}
	if fi.Mode().Perm()&0o077 != 0 {
		return errc(RefuseOwnerOnly, 3)
	}
	uid, _, ok := ownerUID(fi)
	if !ok || uid != uint32(os.Geteuid()) {
		return errc(RefuseOwnerOnly, 3)
	}
	return nil
}

func openRegularNoFollow(path string, maxBytes uint64) ([]byte, error) {
	fi, err := lstatNoFollow(path)
	if err != nil {
		return nil, err
	}
	if err := checkOwnerOnlyFile(fi); err != nil {
		return nil, err
	}
	if uint64(fi.Size()) > maxBytes {
		return nil, errc(LimitExceeded, 1)
	}
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, errc(PathMissing, 1)
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return nil, errc(PathMissing, 1)
	}
	if err := checkOwnerOnlyFile(st); err != nil {
		return nil, err
	}
	if uint64(st.Size()) > maxBytes {
		return nil, errc(LimitExceeded, 1)
	}
	data, err := io.ReadAll(io.LimitReader(f, int64(maxBytes)+1))
	if err != nil {
		return nil, errc(FileIntegrity, 1)
	}
	if uint64(len(data)) > maxBytes {
		return nil, errc(LimitExceeded, 1)
	}
	if int64(len(data)) != st.Size() {
		return nil, errc(FileIntegrity, 1)
	}
	return data, nil
}

func joinUnder(root, rel string, lim Limits) (string, error) {
	if rel == "" || strings.Contains(rel, "\\") {
		return "", errc(PathAllowlist, 1)
	}
	parts := strings.Split(rel, "/")
	if len(parts) == 0 || len(parts) > lim.MaxWalkDepth {
		return "", errc(PathEscape, 1)
	}
	cur := root
	for i, part := range parts {
		if part == "" || part == "." || part == ".." || strings.Contains(part, string(os.PathSeparator)) {
			return "", errc(PathEscape, 1)
		}
		if len(part) > maxComponentBytes {
			return "", errc(PathEscape, 1)
		}
		next := filepath.Join(cur, part)
		fi, err := lstatNoFollow(next)
		if err != nil {
			return "", err
		}
		last := i == len(parts)-1
		if last {
			if err := checkOwnerOnlyFile(fi); err != nil {
				return "", err
			}
		} else {
			if err := checkOwnerOnlyDir(fi); err != nil {
				return "", err
			}
		}
		cur = next
	}
	return cur, nil
}

func readDirNamesNoFollow(path string) ([]string, error) {
	fi, err := lstatNoFollow(path)
	if err != nil {
		return nil, err
	}
	if err := checkOwnerOnlyDir(fi); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, errc(PathMissing, 1)
	}
	defer f.Close()
	names, err := f.Readdirnames(0)
	if err != nil {
		return nil, errc(PathMissing, 1)
	}
	return names, nil
}

func mkdir0700(path string) error {
	if err := os.Mkdir(path, 0o700); err != nil {
		return errc(OutputWrite, 4)
	}
	if err := os.Chmod(path, 0o700); err != nil {
		return errc(OutputWrite, 4)
	}
	return nil
}

func mkdirAll0700(path string) error {
	if path == "" || path == "/" {
		return errc(ArgumentInvalid, 2)
	}
	fi, err := os.Lstat(path)
	if err == nil {
		if fi.Mode()&os.ModeSymlink != 0 {
			return errc(RefuseSymlink, 3)
		}
		if !fi.IsDir() {
			return errc(RefuseNotRegular, 3)
		}
		return nil
	}
	if !os.IsNotExist(err) {
		return errc(OutputWrite, 4)
	}
	parent := filepath.Dir(path)
	if parent != path {
		if err := mkdirAll0700(parent); err != nil {
			return err
		}
	}
	if err := os.Mkdir(path, 0o700); err != nil && !os.IsExist(err) {
		return errc(OutputWrite, 4)
	}
	if err := os.Chmod(path, 0o700); err != nil {
		return errc(OutputWrite, 4)
	}
	fi, err = os.Lstat(path)
	if err != nil {
		return errc(OutputWrite, 4)
	}
	if fi.Mode()&os.ModeSymlink != 0 {
		return errc(RefuseSymlink, 3)
	}
	if !fi.IsDir() {
		return errc(RefuseNotRegular, 3)
	}
	return nil
}

func writeFile0600(path string, data []byte) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return errc(OutputWrite, 4)
	}
	defer f.Close()
	if _, err := f.Write(data); err != nil {
		return errc(OutputWrite, 4)
	}
	if err := f.Chmod(0o600); err != nil {
		return errc(OutputWrite, 4)
	}
	if err := f.Sync(); err != nil {
		return errc(OutputWrite, 4)
	}
	return nil
}

func syncUnsupported(err error) bool {
	return err == syscall.EINVAL || err == syscall.ENOTSUP || err == syscall.EOPNOTSUPP
}

func fsyncPath(path string, dir bool) error {
	if dir && testHook.failDirSync {
		return errc(OutputWrite, 4)
	}
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return errc(OutputWrite, 4)
	}
	defer f.Close()
	if err := f.Sync(); err != nil {
		if dir && syncUnsupported(err) {
			return nil
		}
		return errc(OutputWrite, 4)
	}
	return nil
}

func durableSyncTree(root string) error {
	var dirs []string
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return errc(OutputWrite, 4)
		}
		fi, err := os.Lstat(path)
		if err != nil {
			return errc(OutputWrite, 4)
		}
		if fi.Mode()&os.ModeSymlink != 0 {
			return errc(RefuseSymlink, 3)
		}
		if fi.IsDir() {
			dirs = append(dirs, path)
			return nil
		}
		if !fi.Mode().IsRegular() {
			return errc(RefuseNotRegular, 3)
		}
		return fsyncPath(path, false)
	})
	if err != nil {
		return err
	}
	for i := len(dirs) - 1; i >= 0; i-- {
		if err := fsyncPath(dirs[i], true); err != nil {
			return err
		}
	}
	return nil
}

func stagingName(parent, base string) (string, error) {
	for i := 0; i < 16; i++ {
		var b [8]byte
		if _, err := rand.Read(b[:]); err != nil {
			return "", errc(OutputBusy, 4)
		}
		name := "." + base + ".vault-" + hex.EncodeToString(b[:])
		p := filepath.Join(parent, name)
		if _, err := os.Lstat(p); os.IsNotExist(err) {
			return p, nil
		}
	}
	return "", errc(OutputBusy, 4)
}

func removeAllNoFollow(path string) error {
	fi, err := os.Lstat(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return errc(OutputWrite, 4)
	}
	if fi.Mode()&os.ModeSymlink != 0 {
		return os.Remove(path)
	}
	if !fi.IsDir() {
		return os.Remove(path)
	}
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return errc(OutputWrite, 4)
	}
	names, err := f.Readdirnames(0)
	f.Close()
	if err != nil {
		return errc(OutputWrite, 4)
	}
	for _, name := range names {
		if name == "." || name == ".." {
			continue
		}
		if err := removeAllNoFollow(filepath.Join(path, name)); err != nil {
			return err
		}
	}
	return os.Remove(path)
}

func lockPermissions(root string) error {
	return filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return errc(OutputWrite, 4)
		}
		fi, err := os.Lstat(path)
		if err != nil {
			return errc(OutputWrite, 4)
		}
		if fi.Mode()&os.ModeSymlink != 0 {
			return errc(RefuseSymlink, 3)
		}
		if fi.IsDir() {
			return os.Chmod(path, 0o700)
		}
		return os.Chmod(path, 0o600)
	})
}
