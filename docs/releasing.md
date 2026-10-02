# Releasing

The release history lives in [changelog.md](changelog.md); this file is the
checklist for cutting a release.

## Checklist

1. Update `version` in `pyproject.toml` (semantic versioning).
2. Move the changelog's `Unreleased` entries under the new version and date.
3. Run the full verification:

   ```console
   .venv/bin/python -m pytest -q
   uvx ruff check src tests tools --select F,E9
   ```

4. Review the tree for files that must not be published (`git status`,
   `.gitignore`); official-client artifacts and research binaries stay
   outside version control.
5. Commit the release state.
6. Tag and push:

   ```console
   git tag -a v0.1.0 -m "dasungctl 0.1.0"
   git push origin main --follow-tags
   ```

   The repository needs a remote first:

   ```console
   git remote add origin git@github.com:sal7g86/eink-dasung-control.git
   ```

7. Create the GitHub Release for the tag, using the changelog section as the
   release notes. GitHub attaches the source archives automatically.
