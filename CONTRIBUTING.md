# Contributing

Small, reproducible conversion cases are more useful than large design uploads.

1. Open an issue with the tool versions, conversion direction, expected behavior
   and the smallest **synthetic, nonconfidential** reproducer.
2. Do not attach customer/lab boards, exported libraries, manufacturing data,
   credentials or unsanitized logs. Logs include local paths and net names.
3. Add a regression test with the change. Prefer the existing standard-library
   tools and KiCad interfaces; keep UI automation out of the conversion path.
4. Run the fast checks:

   ```powershell
   python -m unittest discover -s tests -v
   python tools\check_release.py
   ```

5. For conversion changes, install KiCad 9 and 10, then run:

   ```powershell
   .\Fusion_to_Quilter.cmd --input examples\roundtrip-demo
   .\Selftest_Roundtrip.cmd --name roundtrip-demo
   ```

The GitHub workflow runs Python-only checks. It does **not** certify KiCad or
Fusion behavior. State which integration checks you actually ran in your PR.
Keep backwards compatibility with the documented KiCad versions; don't silently
substitute KiCad 10's `pcbnew` for KiCad 9.

Contributions must be yours to submit under [MIT](LICENSE), without confidential
or third-party restricted material. No separate CLA is presently required.
The lab/Penn marks are not available for reuse under the code license.

Treat reports constructively. This is research tooling: describe evidence and
limitations without promising lossless conversion of arbitrary designs.
