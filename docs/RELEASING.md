# Releasing ytmusic-mirror

Cutting a release is: bump the version, tag it, push. CI publishes the
containers and (with a couple of optional secrets) PyPI, GitHub Releases and
the Homebrew tap automatically.

## How each channel is published

| Channel | Who | Trigger |
| --- | --- | --- |
| GHCR (`ghcr.io/suleman-dawood/ytmusic-mirror`) | CI (`.github/workflows/docker.yml`) | auto on `main` (`latest`) and `v*` tags |
| Docker Hub (`sulemandawood/ytmusic-mirror`) | CI (`.github/workflows/dockerhub.yml`) | auto on `main` and `v*` tags |
| GitHub Release + notes | CI (`.github/workflows/release.yml`) | auto on `v*` tags |
| PyPI (`ytmusic-mirror`) | CI (`.github/workflows/release.yml`) | auto on `v*` tags once `PYPI_API_TOKEN` is set |
| Homebrew tap (`suleman-dawood/homebrew-ytmusic`) | CI | auto on `v*` tags once `HOMEBREW_PAT` is set |

## Do a release

```sh
# 1. pick the new version (semver), e.g. 0.9.0
#    - bump `version` in pyproject.toml and ytmusic_mirror/__init__.py

# 2. commit, tag, push — everything downstream is triggered by the tag
git add -A && git commit -m "Release v0.9.0"
git tag v0.9.0
git push && git push origin v0.9.0
```

Then check the Actions tab: `docker`, `dockerhub`, and `release` workflows.

## Optional one-time secrets (per channel)

```sh
# PyPI — auto-publish wheels on tags
#   pypi.org -> Add API token (scope: entire account or ytmusic-mirror)
gh secret set PYPI_API_TOKEN --repo suleman-dawood/ytmusic-mirror

# Homebrew — CI updates the tap formula on tags
#   github.com/settings/tokens -> fine-grained, write access to suleman-dawood/homebrew-ytmusic
gh secret set HOMEBREW_PAT --repo suleman-dawood/ytmusic-mirror
```

Without `PYPI_API_TOKEN`: run `python -m twine upload dist/*` after the
`python -m build` step locally.
Without `HOMEBREW_PAT`: bump the tap manually (edit `url`/`sha256` in
`Formula/ytmusic-mirror.rb`, commit, push).

## Verify after release

```sh
pip index versions ytmusic-mirror        # PyPI
gh release view v0.9.0                    # GitHub
docker manifest inspect ghcr.io/suleman-dawood/ytmusic-mirror:0.9.0
docker manifest inspect sulemandawood/ytmusic-mirror:0.9.0
```
