param(
    [string]$MsysRoot = 'C:\msys64',
    [string]$CondaPython = 'C:\ProgramData\anaconda3\envs\pytorch_env\python.exe',
    [int]$Jobs = 4
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$gem5 = Join-Path $repo 'gem5'
$msysBin = Join-Path $MsysRoot 'usr\bin'
$bash = Join-Path $msysBin 'bash.exe'
$cygpath = Join-Path $msysBin 'cygpath.exe'
$objcopy = Join-Path $msysBin 'objcopy.exe'
$binary = Join-Path $gem5 'build\Garnet_standalone\gem5.opt.exe'
$gem5Commit = '5fa484e2e02604ad3a5bf01f35ad1f97ca6d17b8'

if ($Jobs -lt 1) { throw 'Jobs must be positive' }
if (-not (Test-Path -LiteralPath (Join-Path $gem5 'SConstruct'))) {
    if (Test-Path -LiteralPath $gem5) { throw "Incomplete gem5 directory: $gem5" }
    & git clone -c core.autocrlf=false https://gem5.googlesource.com/public/gem5 $gem5
    if ($LASTEXITCODE -ne 0) { throw 'gem5 clone failed' }
    & git -C $gem5 checkout --detach $gem5Commit
    if ($LASTEXITCODE -ne 0) { throw 'gem5 checkout failed' }
}
$actualCommit = (& git -C $gem5 rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $actualCommit -ne $gem5Commit) {
    throw "gem5 HEAD must be $gem5Commit; got $actualCommit"
}
foreach ($path in @($CondaPython, $bash, $cygpath, $objcopy, (Join-Path $gem5 'SConstruct'))) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required file missing: $path" }
}

foreach ($patch in Get-ChildItem (Join-Path $repo 'gem5_patch') -Filter '0*.patch' | Sort-Object Name) {
    & git -C $gem5 apply --reverse --check $patch.FullName 2>$null
    if ($LASTEXITCODE -eq 0) { continue }
    & git -C $gem5 apply --check $patch.FullName
    if ($LASTEXITCODE -ne 0) { throw "Cannot apply patch: $($patch.FullName)" }
    & git -C $gem5 apply $patch.FullName
    if ($LASTEXITCODE -ne 0) { throw "Patch failed: $($patch.FullName)" }
}

$source = Join-Path $repo 'omelet\backends\gem5\configs'
$destination = Join-Path $gem5 'configs'
Get-ChildItem -LiteralPath $source -File -Recurse | ForEach-Object {
    $relative = [IO.Path]::GetRelativePath($source, $_.FullName)
    $target = Join-Path $destination $relative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $target -Force
}

$toolsDir = Join-Path $repo '.tools'
if (-not (Test-Path (Join-Path $toolsDir 'SCons')) -or
    -not (Test-Path (Join-Path $toolsDir 'pybind11')) -or
    -not (Test-Path (Join-Path $toolsDir 'yaml'))) {
    & $CondaPython -m pip install --target $toolsDir 'scons==4.7.0' 'pybind11==2.13.6' 'PyYAML>=6,<7'
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install build-only Python packages' }
}

$toolsUnix = (& $cygpath -u $toolsDir).Trim()
if ($LASTEXITCODE -ne 0) { throw "Cannot convert MSYS2 path: $toolsDir" }
$oldPath = $env:PATH
$oldMsystem = $env:MSYSTEM
try {
    $env:PATH = "$msysBin;$env:SystemRoot\System32"
    $env:MSYSTEM = 'MSYS'
    Push-Location $gem5
    try {
        $command = "PYTHONPATH='$toolsUnix' /usr/bin/python3 -c 'import SCons.Script; SCons.Script.main()' --ignore-style --no-compress-debug build/Garnet_standalone/gem5.opt.exe -j$Jobs"
        & $bash -c $command
        if ($LASTEXITCODE -ne 0) { throw "gem5 build failed ($LASTEXITCODE)" }
        if (-not (Test-Path -LiteralPath $binary)) { throw "Build did not produce $binary" }
        $stripped = "$binary.stripped"
        & $objcopy --strip-debug $binary $stripped
        if ($LASTEXITCODE -ne 0) { throw 'Failed to strip gem5 debug sections' }
        Move-Item -LiteralPath $stripped -Destination $binary -Force
    } finally {
        Pop-Location
    }
} finally {
    $env:PATH = $oldPath
    $env:MSYSTEM = $oldMsystem
}

Write-Output "gem5 ready: $binary"
