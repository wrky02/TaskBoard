# Publish only the explicitly generated synthetic screenshots, never app data.
$ErrorActionPreference = 'Stop'
$preview = Join-Path $env:RUNNER_TEMP 'taskboard-ui-preview'
$branch = 'build/native-ui-preview'
$header = git config --get http.https://github.com/.extraheader
if (-not $header) { throw 'Checkout authentication is unavailable' }
New-Item -ItemType Directory -Force $preview | Out-Null
git -C $preview init -q
git -C $preview remote add origin "https://github.com/$env:GITHUB_REPOSITORY.git"
$existing = git -C $preview -c "http.extraheader=$header" ls-remote --heads origin "refs/heads/$branch"
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect preview branch' }
if ($existing) {
  git -C $preview -c "http.extraheader=$header" fetch --depth=1 origin $branch
  git -C $preview checkout -B $branch FETCH_HEAD
} else {
  git -C $preview checkout --orphan $branch
}
if ($LASTEXITCODE -ne 0) { throw 'Cannot prepare preview branch' }
Copy-Item artifacts/visual-review/*.png $preview -Force
Copy-Item artifacts/visual-review/README.md $preview -Force
git -C $preview config user.name 'github-actions[bot]'
git -C $preview config user.email '41898282+github-actions[bot]@users.noreply.github.com'
git -C $preview add -- '*.png' README.md
git -C $preview diff --cached --quiet
if ($LASTEXITCODE -eq 1) {
  git -C $preview commit -m "Native UI preview for $env:GITHUB_SHA [skip ci]"
  if ($LASTEXITCODE -ne 0) { throw 'Cannot commit preview images' }
  git -C $preview -c "http.extraheader=$header" push origin "HEAD:refs/heads/$branch"
  if ($LASTEXITCODE -ne 0) { throw 'Cannot publish preview images' }
}
