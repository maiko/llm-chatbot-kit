# Packaging

CI builds source and wheel distributions on every push to `main` and on tags (`v*`).

Artifacts
- Build workflow: `.github/workflows/package.yml`
- Outputs: `dist/*.tar.gz` (sdist) and `dist/*.whl` (wheel)
- Download: from the Actions run artifacts named `package-dist`

Releases
- Tag with `vX.Y.Z` to create a GitHub Release automatically with the built artifacts attached.

PyPI (optional)
- Add repository secret `PYPI_API_TOKEN` to publish on tagged builds.
- The workflow will skip publishing if the secret is absent.

Local build
```
python -m pip install --upgrade build
python -m build
```
