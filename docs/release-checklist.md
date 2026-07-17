# Public release checklist

Use this checklist before making AquaTag public.

## Project identity

- [ ] Replace anonymous manuscript metadata with the final author list and affiliations.
- [ ] Add the publication venue, year, DOI, paper URL, and final BibTeX.
- [ ] Add repository owner/contact and acknowledgments.
- [ ] Add a teaser image with confirmed publication permission and alt text.

## Licensing and attribution

- [ ] Choose and add explicit licenses for code, hardware design, and documentation/media.
- [ ] Confirm that third-party files can be redistributed and retain their notices.
- [ ] Add `CITATION.cff` after authors, title, DOI, and repository URL are final.

## Reproducibility

- [ ] Upload editable sources as well as manufacturing/export formats.
- [ ] Pin toolchain and dependency versions; include lockfiles where supported.
- [ ] Publish sample input and expected output for every major component.
- [ ] Record compatible hardware, firmware, protocol, and app versions.
- [ ] Build the firmware and both applications from a clean checkout.
- [ ] Add checksums and provenance for release binaries and manufacturing packages.

## Hardware and safety

- [ ] Reconcile BOM part numbers, substitutions, quantities, and pricing with the final tested revision.
- [ ] Publish assembly, potting, charging-contact care, inspection, and pressure-test procedures.
- [ ] Clearly distinguish tested results from certification, rated limits, and untested variants.
- [ ] Add battery/resin safety guidance and required PPE.

## GitHub publication

- [ ] Use Git LFS or versioned releases for large CAD, media, and firmware artifacts.
- [ ] Configure issue/discussion settings and a private security/safety contact.
- [ ] Add repository topics, description, social preview, and archived release assets.
- [ ] Create a tagged, immutable first release and test all README links from GitHub.
