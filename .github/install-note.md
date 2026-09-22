
---

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

**Windows**: SmartScreen may warn on first run. Choose More info, then Run
anyway. Installs per-user, no admin needed.

</details>
