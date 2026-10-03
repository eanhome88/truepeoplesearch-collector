[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string]$InstallRoot = 'D:\TruePeopleSearch',
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{40}$')]
    [string]$ExpectedPythonSignerThumbprint
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

$root = Get-NormalizedInstallRoot $InstallRoot
Assert-TpsSupportedWindowsHost
$null = Assert-TpsProtectedDirectoryAcl $root
$environmentPath = Join-Path $root 'config\runtime.env'
$configuration = Read-TpsRuntimeEnvironment $environmentPath $root
Set-TpsProcessEnvironment $configuration
Assert-TpsBusinessStorageOnD $root
$appRoot = Get-TpsAppRoot $root
$bundleVerifier = Join-Path $appRoot 'deploy\windows-full\Test-FullBundle.ps1'
& $bundleVerifier -BundleRoot $root -AllowRuntimeState

$pythonInstaller = Join-Path $root 'vendor\python\python-3.12.10-amd64.exe'
$wheelhouse = Join-Path $root 'vendor\wheelhouse'
$browserArchive = Join-Path $root 'vendor\browser\chromium-1243.zip'
$pythonRoot = Join-Path $root 'runtime\python'
$venvRoot = Join-Path $root 'runtime\.venv'
$pythonExe = Join-Path $pythonRoot 'python.exe'
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'
$requirements = Join-Path $appRoot 'deploy\windows-full\requirements-full.txt'
$browserRoot = Join-Path $root 'runtime\ms-playwright'
$browserVersionRoot = Join-Path $browserRoot 'chromium-1243'
$browserMarker = Join-Path $browserVersionRoot 'INSTALLATION_COMPLETE'
$headlessVersionRoot = Join-Path $browserRoot 'chromium_headless_shell-1243'
$headlessMarker = Join-Path $headlessVersionRoot 'INSTALLATION_COMPLETE'
$runtimeManifestPath = Join-Path $root 'runtime\runtime-manifest.json'

$runtimeAlreadyInstalled = Test-Path -LiteralPath $runtimeManifestPath -PathType Leaf
if ($runtimeAlreadyInstalled) {
    Assert-TpsRuntimeManifest $root
}
elseif ((Test-Path -LiteralPath $pythonRoot) -or
        (Test-Path -LiteralPath $venvRoot) -or
        (Test-Path -LiteralPath $browserVersionRoot) -or
        (Test-Path -LiteralPath $headlessVersionRoot)) {
    throw 'An unverified or partial managed runtime already exists. Move it aside and reinstall from the verified bundle.'
}

foreach ($required in @($pythonInstaller, $wheelhouse, $browserArchive, $requirements)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Offline runtime asset is missing: $required"
    }
    Assert-NoReparsePoint $required
}

if (-not $runtimeAlreadyInstalled) {
    $signature = Get-AuthenticodeSignature -LiteralPath $pythonInstaller
    $expected = $ExpectedPythonSignerThumbprint.ToUpperInvariant()
    if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or
        $signature.SignerCertificate.Thumbprint.ToUpperInvariant() -ne $expected) {
        throw 'The Python installer does not match the approved Authenticode publisher.'
    }
    if ($PSCmdlet.ShouldProcess($pythonRoot, 'Install the verified Python runtime on D:')) {
        $arguments = @(
            '/quiet', 'InstallAllUsers=0', 'Include_launcher=0', 'Include_test=0',
            'Include_pip=1', 'Include_doc=0', 'Shortcuts=0', 'PrependPath=0',
            ('TargetDir=' + $pythonRoot)
        )
        $process = Start-Process -FilePath $pythonInstaller -ArgumentList $arguments -Wait -PassThru
        if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
            throw "Verified Python installation failed with exit code $($process.ExitCode)."
        }
    }
}

& $pythonExe -I -B -c "import platform, sys; raise SystemExit(0 if sys.version_info[:3] == (3, 12, 10) and platform.machine().lower() in {'amd64', 'x86_64'} else 1)"
if ($LASTEXITCODE -ne 0) {
    throw 'The D-drive Python runtime is not the approved Python 3.12.10 x64 target.'
}

if (-not $runtimeAlreadyInstalled) {
    if ($PSCmdlet.ShouldProcess($venvRoot, 'Create the D-drive Python virtual environment')) {
        & $pythonExe -I -B -m venv $venvRoot
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not create the offline Python virtual environment.'
        }
    }
}

if (-not $runtimeAlreadyInstalled) {
    if ($PSCmdlet.ShouldProcess($venvRoot, 'Install Python dependencies from the verified offline wheelhouse')) {
        & $venvPython -I -B -m pip --isolated install --disable-pip-version-check --no-cache-dir --no-index --only-binary=:all: --find-links $wheelhouse -r $requirements
        if ($LASTEXITCODE -ne 0) {
            throw 'Offline Python dependency installation failed.'
        }
    }
}

if (-not $runtimeAlreadyInstalled) {
    if ((Test-Path -LiteralPath $browserVersionRoot) -or (Test-Path -LiteralPath $headlessVersionRoot)) {
        throw 'A partial Chromium runtime already exists; refusing to merge into it.'
    }
    Assert-NoReparsePoint $browserRoot
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($browserArchive)
    try {
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName.Replace('\', '/')
            if (-not $name -or $name.StartsWith('/') -or $name -match '^[A-Za-z]:' -or $name -match '(^|/)\.\.(/|$)') {
                throw 'The Chromium archive contains an unsafe path.'
            }
        }
    }
    finally {
        $archive.Dispose()
    }
    if ($PSCmdlet.ShouldProcess($browserRoot, 'Expand the verified Chromium runtime on D:')) {
        $browserStage = Join-Path (Join-Path $root 'runtime') ('.chromium-stage-' + [Guid]::NewGuid().ToString('N'))
        try {
            Expand-Archive -LiteralPath $browserArchive -DestinationPath $browserStage
            $stagedVersion = Join-Path $browserStage 'chromium-1243'
            $stagedHeadless = Join-Path $browserStage 'chromium_headless_shell-1243'
            if (-not (Test-Path -LiteralPath (Join-Path $stagedVersion 'INSTALLATION_COMPLETE') -PathType Leaf) -or
                -not (Test-Path -LiteralPath (Join-Path $stagedVersion 'chrome-win64\chrome.exe') -PathType Leaf) -or
                -not (Test-Path -LiteralPath (Join-Path $stagedHeadless 'INSTALLATION_COMPLETE') -PathType Leaf) -or
                -not (Test-Path -LiteralPath (Join-Path $stagedHeadless 'chrome-headless-shell-win64\chrome-headless-shell.exe') -PathType Leaf)) {
                throw 'The expanded Chromium runtime is incomplete.'
            }
            Move-Item -LiteralPath $stagedVersion -Destination $browserVersionRoot
            Move-Item -LiteralPath $stagedHeadless -Destination $headlessVersionRoot
        }
        finally {
            if ($browserStage -and (Test-Path -LiteralPath $browserStage)) {
                Remove-Item -LiteralPath $browserStage -Recurse -Force
            }
        }
    }
}

& $venvPython -I -B -c 'import flask, mysql.connector, redis, psutil, scrapling, curl_cffi, httpx'
if ($LASTEXITCODE -ne 0) {
    throw 'The installed Python runtime failed its import check.'
}
& $venvPython -I -B -m pip --isolated check
if ($LASTEXITCODE -ne 0) {
    throw 'The installed offline Python dependency closure is inconsistent.'
}

$browserSmoke = @'
from playwright.sync_api import sync_playwright
from patchright.sync_api import sync_playwright as sync_patchright

for launcher in (sync_playwright, sync_patchright):
    with launcher() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("data:text/html,<title>offline-smoke</title>")
        if page.title() != "offline-smoke":
            raise RuntimeError("offline Chromium page smoke failed")
        browser.close()
'@
& $venvPython -I -B -c $browserSmoke
if ($LASTEXITCODE -ne 0) {
    throw 'The pinned Chromium revision failed its offline Playwright/Patchright launch smoke.'
}

if (-not $runtimeAlreadyInstalled) {
    Write-TpsRuntimeManifest $root
    Assert-TpsRuntimeManifest $root
}

$mysqlImage = Join-Path $root 'vendor\images\mysql-8.4.11-linux-amd64.tar'
$redisImage = Join-Path $root 'vendor\images\redis-7.4.8-alpine-linux-amd64.tar'
foreach ($image in @($mysqlImage, $redisImage)) {
    if (-not (Test-Path -LiteralPath $image -PathType Leaf)) {
        throw "Offline Docker image is missing: $image"
    }
}
$dockerTools = Get-TpsDockerTools $root
$docker = $dockerTools.Docker
& $docker --context desktop-linux version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Desktop is not running in Linux-container mode.'
}
Assert-TpsDockerStorageOnD $root
foreach ($image in @($mysqlImage, $redisImage)) {
    if ($PSCmdlet.ShouldProcess($image, 'Load verified offline Docker image')) {
        & $docker --context desktop-linux load --input $image
        if ($LASTEXITCODE -ne 0) {
            throw "Docker image load failed: $image"
        }
    }
}
[void](Get-TpsVerifiedDockerImageIds $root)

Write-Host 'Offline runtime preparation completed.'
Write-Host 'No database, queue, dashboard, or collection job was started.'
