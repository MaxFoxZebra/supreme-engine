
---

**Claude Desktop only, no app?** Download the `.mcpb` for your computer
(`CV-Studio-…-macos-arm64.mcpb` for Apple silicon, `-macos-x64` for Intel
Macs, `-windows-x64` for Windows) and double-click it: Claude Desktop installs
CV Studio as an extension and asks which folder to keep your CVs in. Pick
`Documents/CV Studio` to share it with the desktop app. Use the extension or
the app's *Connect to Claude*, not both, or Claude sees every tool twice.

<details>
<summary>First launch needs one extra step</summary>

Not notarized by Apple or signed by Microsoft.

**macOS**: open the disk image, drag CV Studio to Applications, then run this
once:

```
xattr -dr com.apple.quarantine "/Applications/CV Studio.app"
```

After that it opens by double-click like anything else. Without it macOS
reports the app as *damaged and can't be opened*, which is what it says about
any app that was downloaded and is not notarized. The app is not damaged, and
the command does not disable anything system-wide: it clears the flag macOS
attached to this one download.

Right-click then Open used to work instead and no longer does. macOS 15 removed
it, so on Sequoia and later the command above is the way in.

**The extension on macOS**: if Claude Desktop says the CV Studio server
could not start, clear the same flag on it once:

```
xattr -dr com.apple.quarantine "$HOME/Library/Application Support/Claude/Claude Extensions"
```

**Windows**: SmartScreen may warn on first run. Choose More info, then Run
anyway. Installs per-user, no admin needed.

</details>
