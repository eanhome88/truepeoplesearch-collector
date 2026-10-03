[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string]$InstallRoot = 'D:\TruePeopleSearch',
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{40}$')]
    [string]$ExpectedDockerSignerThumbprint
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

$root = Get-NormalizedInstallRoot $InstallRoot
Assert-TpsSupportedWindowsHost
Assert-TpsAdministrator
$appRoot = Get-TpsAppRoot $root
& (Join-Path $appRoot 'deploy\windows-full\Test-FullBundle.ps1') -BundleRoot $root -AllowRuntimeState
Assert-TpsBusinessStorageOnD $root
Assert-TpsProtectedDirectoryAcl (Join-Path $root 'runtime\docker-data')
Assert-TpsStorageDirectoryTree (Join-Path $root 'runtime\docker-data')

$installer = Join-Path $root 'vendor\docker\DockerDesktopInstaller.exe'
$dataRoot = Join-Path $root 'runtime\docker-data'
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw 'Verified Docker Desktop installer is missing.'
}
Assert-NoReparsePoint $installer
Assert-NoReparsePoint $dataRoot
$signature = Get-AuthenticodeSignature -LiteralPath $installer
$expected = $ExpectedDockerSignerThumbprint.ToUpperInvariant()
if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or
    $signature.SignerCertificate.Thumbprint.ToUpperInvariant() -ne $expected) {
    throw 'The Docker Desktop installer does not match the approved Authenticode publisher.'
}

if ($PSCmdlet.ShouldProcess('Docker Desktop with WSL data under D:', 'Install verified dependency runtime')) {
    $arguments = @(
        'install', '--quiet', '--backend=wsl-2', '--no-windows-containers',
        ('--wsl-default-data-root=' + $dataRoot)
    )
    $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Docker Desktop installation failed with exit code $($process.ExitCode)."
    }
    # Bind the installed binaries to the exact Desktop/CLI/Compose versions
    # and publisher recorded by the reviewed offline bundle before declaring
    # the host dependency installation successful.
    [void](Get-TpsDockerTools $root)
}

Write-Host 'Docker Desktop installation completed with its WSL data root targeted to D:.'
Write-Host 'Start Docker Desktop, accept its license if applicable, then run Install-OfflineRuntime.ps1.'
Write-Host 'No image, container, database, queue, dashboard, or collector was started.'
