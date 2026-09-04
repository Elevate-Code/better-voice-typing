# Release checklist

The automated part (`.github/workflows/release.yml`) runs on a `v*` tag:
tests → PyInstaller one-folder build → Inno Setup installer → `SHA256SUMS.txt`
→ GitHub Release with notes generated from `CHANGELOG.json`.

## Before tagging

- [ ] `CHANGELOG.json`: the top entry has `"version": "X.Y.Z"`.
- [ ] `version.txt` and `pyproject.toml` both say `X.Y.Z` (`tests/test_version.py` guards this).
- [ ] `uv sync` is clean and `uv run pytest` passes locally.
- [ ] Local build works: `uv run pyinstaller BetterVoiceTyping.spec --noconfirm`, then
      `iscc /DAppVersion=X.Y.Z installer\BetterVoiceTyping.iss` and run `dist\installer\BetterVoiceTyping-Setup-X.Y.Z.exe`.

## Manual smoke test on the built app (not covered by the test suite)

Run these on the installed build, ideally on a clean Windows VM or a second user account:

- [ ] Installer: SmartScreen "More info → Run anyway" works; installs without admin; Start menu entry and startup task present.
- [ ] First launch: tray icon appears; "Open API Keys" opens `Documents\VoiceTyping\.env`; after adding a key and Restart, a dictation pastes text.
- [ ] Hotkey: Caps Lock mash and hold produce one toggle each; Ctrl+Caps toggles real Caps Lock.
- [ ] Paste: Notepad, a browser text field, a terminal.
- [ ] Meeting mode (needs ElevenLabs): a two-chunk session delivers in order with the preamble once.
- [ ] Update path: with an older installed version, tray → Check for Updates downloads, installs silently and relaunches; settings and history intact.
- [ ] Uninstall via Settings → Apps: program folder gone, `Documents\VoiceTyping` untouched.

## Tag and publish

```powershell
git tag vX.Y.Z
git push origin master vX.Y.Z
```

Watch the Release workflow; when it finishes, open the release page, check the
notes and the two assets, and do the update-path smoke test from the previous
version if you skipped it above.
