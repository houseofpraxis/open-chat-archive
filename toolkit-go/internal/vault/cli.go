package vault

import (
	"flag"
	"io"
	"sort"
)

const helpText = `oca-vault — offline Markdown vault renderer (v0.1-experimental)

Renders one Python-attested Open Chat Archive v0.1 directory into a dedicated
owner-only Markdown tree. Normative validation stays in the Python attestation.
oca-vault does not revalidate Draft 2020-12 JSON Schema, parse vendor source
formats, execute plugins or clients, or read sources/, blobs/, import-manifests/,
or registry files.

Usage:
  oca-vault build --archive ABS --attestation ABS --output ABS
  oca-vault --version
  oca-vault --help

Flags for build:
  --archive ABS        absolute archive directory (opaque archive_id name)
  --attestation ABS    absolute oca-validation-attestation/v0.1-experimental file
  --output ABS         absolute dedicated output directory

Output is restricted-unclassified. Directories 0700, files 0600. The marker
.open-chat-archive-vault-output identifies a replaceable generated tree.
Default stderr is a single JSON object with a fixed error code.

Exit: 0 ok; 1 attestation/archive; 2 argument; 3 refuse overlap/unowned/symlink;
4 output write.
`

func Main(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		writeErr(stderr, ArgumentInvalid)
		return 2
	}
	switch args[0] {
	case "--help", "-help", "help", "-h":
		_, _ = io.WriteString(stdout, helpText)
		return 0
	case "--version", "-version", "version":
		writeJSON(stdout, map[string]string{
			"generated_by": GeneratedBy,
			"version":      Version,
		})
		return 0
	case "build":
		if len(args) > 1 && (args[1] == "--help" || args[1] == "-h") {
			_, _ = io.WriteString(stdout, helpText)
			return 0
		}
		archive, attestation, output, err := parseBuildFlags(args[1:])
		if err != nil {
			writeErr(stderr, ErrorCode(err))
			return ExitCode(err)
		}
		if err := Build(archive, attestation, output); err != nil {
			writeErr(stderr, ErrorCode(err))
			return ExitCode(err)
		}
		writeJSON(stdout, map[string]string{
			"generated_by": GeneratedBy,
			"operation":    "build",
			"status":       "ok",
		})
		return 0
	default:
		writeErr(stderr, ArgumentInvalid)
		return 2
	}
}

func parseBuildFlags(args []string) (archive, attestation, output string, err error) {
	fs := flag.NewFlagSet("build", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	fs.Usage = func() {}
	fs.StringVar(&archive, "archive", "", "")
	fs.StringVar(&attestation, "attestation", "", "")
	fs.StringVar(&output, "output", "", "")
	if err := fs.Parse(args); err != nil {
		return "", "", "", errc(ArgumentInvalid, 2)
	}
	if fs.NArg() != 0 || archive == "" || attestation == "" || output == "" {
		return "", "", "", errc(ArgumentInvalid, 2)
	}
	return archive, attestation, output, nil
}

func writeJSON(w io.Writer, obj map[string]string) {
	keys := make([]string, 0, len(obj))
	for k := range obj {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	m := make(map[string]any, len(obj))
	for _, k := range keys {
		m[k] = obj[k]
	}
	raw, err := CanonicalBytes(m)
	if err != nil {
		return
	}
	_, _ = w.Write(raw)
	_, _ = w.Write([]byte{'\n'})
}

func writeErr(w io.Writer, code string) {
	writeJSON(w, map[string]string{"error": code})
}
