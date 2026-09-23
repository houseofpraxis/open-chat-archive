package vault

// Build renders one Python-attested v0.1 archive into a dedicated Markdown tree.
// It does not revalidate Draft 2020-12 JSON Schema, parse vendor formats, or
// open sources/, blobs/, import-manifests/, or registry files.
func Build(archive, attestation, output string) error {
	return buildWithLimits(archive, attestation, output, DefaultLimits())
}

func buildWithLimits(archive, attestation, output string, lim Limits) error {
	archive, err := requireAbs(archive)
	if err != nil {
		return err
	}
	attestation, err = requireAbs(attestation)
	if err != nil {
		return err
	}
	output, err = requireAbs(output)
	if err != nil {
		return err
	}
	if overlaps(archive, attestation) || overlaps(archive, output) || overlaps(attestation, output) {
		return errc(RefuseInputOutputOverlap, 3)
	}
	if _, err := lstatNoFollow(archive); err != nil {
		return err
	}
	att, err := loadAttestationFile(attestation, lim)
	if err != nil {
		return err
	}
	arch, err := loadArchive(archive, att, lim)
	if err != nil {
		return err
	}
	sessions, err := projectSessions(arch, lim)
	if err != nil {
		return err
	}
	staging, err := prepareOutput(output, []string{archive, attestation})
	if err != nil {
		return err
	}
	defer func() {
		if staging != "" {
			_ = removeAllNoFollow(staging)
		}
	}()
	if err := writeVaultTree(staging, att.ArchiveID, arch, sessions, lim); err != nil {
		return err
	}
	if err := installOutput(staging, output); err != nil {
		return err
	}
	staging = ""
	return nil
}
