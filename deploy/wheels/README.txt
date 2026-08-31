Tracked win_amd64 wheels for the offline release. Every file here is hashed in
requirements-release.lock; install.ps1 installs with --no-index --require-hashes,
so an untracked or unhashed wheel cannot enter the graph. Test and lint wheels
are deliberately absent - the fab target never runs them.

To refresh (Windows, CPython 3.11 x64), from the repository root:

  python -m pip download -r requirements-release.lock `
      --require-hashes `
      --platform win_amd64 `
      --python-version 311 `
      --only-binary :all: `
      --dest deploy\wheels

Adding a dependency means: pin it in requirements.txt, add its hash to
requirements-release.lock, and download its wheel here in the same change.
