# Vendored identity contract

These files are verbatim copies of the published identity assertion contract
artifacts. Nothing in this repository imports code from the producer.

- Producer repository: clagentic/clagentic-gatekeeper
- Pinned commit: 8fe103264be8156172da794fd15f2126fbc43cbf
- Contract version: 2.0.0 (assertion majors 1 and 2)

| Local path | Source path | Git blob |
|------------|-------------|----------|
| schemas/identity-assertion.json | schemas/identity-assertion.json | f1e28034ae398d75168e96f51ad9f0ada2be5038 |
| schemas/identity-assertion-2.json | schemas/identity-assertion-2.json | 88158e641e5c4c53c84998c560d29d979369fff2 |
| schemas/jwks-discovery.json | schemas/jwks-discovery.json | 9cbcf58b1ff3771604ac6f67a96724a911a6d42f |
| vectors/v1/jwks.json | vectors/v1/jwks.json | f25214cd663668236e9cf18b7e001817e3f0a27c |
| vectors/v1/manifest.json | vectors/v1/manifest.json | 0e70e73c08fc2edb9d990681196f6c1230c6f9c6 |
| vectors/v2/jwks.json | vectors/v2/jwks.json | c725f72bf7a0d7be7afb007e44f8d8193811b0d2 |
| vectors/v2/manifest.json | vectors/v2/manifest.json | 4f48fba33c122ffd9a2c5d366b05bc773368fb63 |

To update, replace the files from a newer pinned commit, update this table and
the commit above, and re-run the identity tests.
