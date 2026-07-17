# Contributing to AquaTag

Thank you for helping make AquaTag reproducible and useful to the aquatic sensing community.

## Before contributing

The initial source release is still in preparation. Until it is published, use issues to discuss substantial changes and avoid treating placeholder interfaces as stable.

## Where files belong

- Electrical design and manufacturing outputs: `hardware/electronics/`
- Enclosure, clip, mount, and print files: `hardware/mechanical/`
- Assembly, potting, and validation procedures: `hardware/fabrication/`
- Embedded source and device protocol: `firmware/`
- Annotation and mobile applications: `software/`
- Reproducible demonstrations: `examples/`

Update the nearest README whenever adding a new dependency, file format, build step, or safety-relevant procedure.

## Pull-request checklist

- The change has a clear purpose and is placed in the appropriate folder.
- Build, test, or validation commands are documented and pass locally.
- Hardware revisions identify compatible firmware and enclosure versions.
- Generated artifacts can be traced to their editable source files.
- Large binary files use Git LFS or a versioned GitHub Release when appropriate.
- No credentials, signing keys, unique device identifiers, or private recordings are included.
- Safety limitations and required equipment are documented without overstating validation or certification.

## Reporting problems

For software and documentation problems, open a GitHub issue with reproduction steps and relevant versions. Do not publicly disclose a safety-critical hardware failure or security vulnerability; use the repository owner's private contact method once it is published.
