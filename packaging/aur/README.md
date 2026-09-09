# Publishing ytmusic-mirror to the AUR

These files are the canonical AUR package source for reference. The final push
goes to `aur.archlinux.org` (which requires your own AUR account + SSH key —
it cannot be done from CI).

## Prerequisites

1. Create an account at https://aur.archlinux.org
2. Add your SSH public key in *Account → SSH Keys*

## Submit

```sh
# 1. Test build locally (do this on an Arch box, or with makepkg/paru/yay)
makepkg -si          # uses this PKGBUILD + .SRCINFO

# 2. Clone the empty AUR package repo and copy the files in
git clone ssh://aur@aur.archlinux.org/ytmusic-mirror
cd ytmusic-mirror
cp /path/to/repo/packaging/aur/PKGBUILD .
cp /path/to/repo/packaging/aur/.SRCINFO .

# 3. Sanity check that .SRCINFO matches the PKGBUILD
makepkg --printsrcinfo > .SRCINFO && git diff --stat

# 4. Publish
git add PKGBUILD .SRCINFO
git commit -m "ytmusic-mirror v0.8.0"
git push origin master
```

## Updating for a new release

```sh
pkgver=0.9.0                    # bump in PKGBUILD
updpkgsums                      # refresh sha256sums from the new tarball
makepkg --printsrcinfo > .SRCINFO
git add PKGBUILD .SRCINFO && git commit -m "ytmusic-mirror v0.9.0" && git push
```

## Notes

- Arch users install with an AUR helper: `paru -S ytmusic-mirror` or
  `yay -S ytmusic-mirror`.
- `python-setuptools` is only a build dependency (the wheel is built
  offline with `--no-isolation`).
- The `[web]` extras (fastapi/uvicorn/apscheduler) are listed as optdepends;
  users who want the dashboard install them separately.
