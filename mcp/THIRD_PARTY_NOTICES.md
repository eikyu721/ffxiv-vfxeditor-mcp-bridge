# Third-party references

The offline Havok tagfile reader in `havok_tagfile.py` is an independent Python implementation informed by the binary-tagfile parsing model in [exyorha/hkxparse](https://github.com/exyorha/hkxparse), which is MIT licensed. Its source and license were reviewed during implementation; no native binary or runtime dependency is redistributed.

FFXIV-specific field expectations were cross-checked against the public [Zee 010 Templates Havok_HKX.bt](https://github.com/CucFlavius/Zee-010-Templates/blob/main/Havok_HKX.bt) and the existing VFXEditor structures. Unsupported structures remain explicitly reported rather than guessed.
