Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:AllowedRuntimeKeys = @(
    'TPS_INSTALL_ROOT',
    'TPS_DB_HOST',
    'TPS_DB_PORT',
    'TPS_DB_NAME',
    'TPS_DB_USER',
    'TPS_DB_PASSWORD',
    'TPS_MYSQL_ROOT_PASSWORD',
    'TPS_REDIS_HOST',
    'TPS_REDIS_PORT',
    'TPS_REDIS_PASSWORD',
    'TPS_DASHBOARD_PORT',
    'TPS_TIMEZONE',
    'PLAYWRIGHT_BROWSERS_PATH',
    'CLOUDBYPASS_PROXY',
    'PROXY_TUNNEL',
    'PROXY_FILE',
    'TPS_CONCURRENCY'
)

function Get-NormalizedInstallRoot {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
        throw 'InstallRoot is required.'
    }
    $full = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\', '/')
    $root = [IO.Path]::GetPathRoot($full)
    if ($root -notmatch '^[dD]:\\$') {
        throw 'The full runtime must be installed on drive D:.'
    }
    if (-not $full.Equals('D:\TruePeopleSearch', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'InstallRoot must be exactly D:\TruePeopleSearch.'
    }
    return $full
}

function Assert-NoReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)

    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor -and (Test-Path -LiteralPath $cursor)) {
        $item = Get-Item -LiteralPath $cursor -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Reparse points are not permitted in the runtime path: $cursor"
        }
        $parent = Split-Path -Parent $cursor
        if (-not $parent -or $parent -eq $cursor) {
            break
        }
        $cursor = $parent
    }
}

function Get-TpsNativeSystemToolPath {
    param([Parameter(Mandatory = $true)][ValidateSet('icacls.exe', 'wsl.exe')][string]$Name)

    if (-not [Environment]::Is64BitProcess) {
        throw 'A native 64-bit PowerShell process is required for Windows system tools.'
    }
    $systemDirectory = [Environment]::SystemDirectory
    if ([string]::IsNullOrWhiteSpace($systemDirectory)) {
        throw 'The native Windows System32 directory could not be resolved.'
    }
    $candidate = Join-Path $systemDirectory $Name
    $item = Get-Item -LiteralPath $candidate -Force -ErrorAction Stop
    if (-not ($item -is [IO.FileInfo]) -or
        (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw "Native Windows system tool is not a regular file: $candidate"
    }
    Assert-NoReparsePoint $candidate
    return [IO.Path]::GetFullPath($candidate)
}

function Assert-TpsStorageDirectoryTree {
    param([Parameter(Mandatory = $true)][string]$Path)

    $root = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw "Required D-drive storage directory is missing: $root"
    }
    Assert-NoReparsePoint $root
    $rootPrefix = $root + '\'
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($root)
    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop) {
            $full = [IO.Path]::GetFullPath($item.FullName)
            if (-not $full.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Storage entry escaped its approved D-drive tree: $full"
            }
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Reparse points are not permitted in persistent storage: $full"
            }
            if ($item.PSIsContainer) {
                $pending.Push($full)
            }
        }
    }
}

function Assert-TpsBusinessStorageOnD {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    $root = Get-NormalizedInstallRoot $InstallRoot
    foreach ($relative in @('backups', 'data\mysql', 'data\redis', 'logs', 'runtime\temp')) {
        $path = Join-Path $root $relative
        Assert-TpsProtectedDirectoryAcl $path
        Assert-TpsStorageDirectoryTree $path
    }
}

function Convert-ToComposePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return ([IO.Path]::GetFullPath($Path).TrimEnd('\', '/') -replace '\\', '/')
}

function Convert-ToApprovedPort {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$Name
    )
    $parsed = 0
    if (-not [int]::TryParse($Value, [ref]$parsed) -or $parsed -lt 1 -or $parsed -gt 65535) {
        throw "$Name must be an integer from 1 to 65535."
    }
    return $parsed
}

function Read-TpsRuntimeEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$ExpectedInstallRoot
    )

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (-not ($item -is [IO.FileInfo]) -or
        (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) -or
        $item.Length -gt 65536) {
        throw 'runtime.env must be a regular file no larger than 64 KiB.'
    }
    Assert-TpsProtectedFileAcl $Path

    $result = @{}
    foreach ($line in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) {
            continue
        }
        $match = [regex]::Match($trimmed, '^([A-Z][A-Z0-9_]*)=(.*)$')
        if (-not $match.Success) {
            throw 'runtime.env contains an invalid line.'
        }
        $name = $match.Groups[1].Value
        $value = $match.Groups[2].Value
        if ($script:AllowedRuntimeKeys -notcontains $name) {
            throw "runtime.env contains an unsupported key: $name"
        }
        if ($result.ContainsKey($name)) {
            throw "runtime.env contains a duplicate key: $name"
        }
        if ([string]::IsNullOrWhiteSpace($value) -or $value.IndexOfAny([char[]]@(0, 10, 13)) -ge 0) {
            throw "runtime.env contains an invalid value for $name."
        }
        $result[$name] = $value
    }

    foreach ($required in @(
        'TPS_INSTALL_ROOT', 'TPS_DB_HOST', 'TPS_DB_PORT', 'TPS_DB_NAME',
        'TPS_DB_USER', 'TPS_DB_PASSWORD', 'TPS_MYSQL_ROOT_PASSWORD',
        'TPS_REDIS_HOST', 'TPS_REDIS_PORT', 'TPS_REDIS_PASSWORD', 'TPS_DASHBOARD_PORT',
        'TPS_TIMEZONE', 'PLAYWRIGHT_BROWSERS_PATH'
    )) {
        if (-not $result.ContainsKey($required)) {
            throw "runtime.env is missing required key: $required"
        }
    }

    foreach ($secretName in @('TPS_DB_PASSWORD', 'TPS_MYSQL_ROOT_PASSWORD', 'TPS_REDIS_PASSWORD')) {
        if ([string]$result[$secretName] -notmatch '^[A-Fa-f0-9]{64}$') {
            throw "$secretName must be a locally generated 64-character hexadecimal secret."
        }
    }

    $expectedRoot = Convert-ToComposePath (Get-NormalizedInstallRoot $result['TPS_INSTALL_ROOT'])
    if ($result['TPS_INSTALL_ROOT'].TrimEnd('/') -ne $expectedRoot) {
        throw 'TPS_INSTALL_ROOT must use the normalized D:/TruePeopleSearch form.'
    }
    if ($ExpectedInstallRoot) {
        $invokedRoot = Convert-ToComposePath (Get-NormalizedInstallRoot $ExpectedInstallRoot)
        if ($expectedRoot -ne $invokedRoot) {
            throw 'runtime.env belongs to a different install root.'
        }
    }
    if ($result['PLAYWRIGHT_BROWSERS_PATH'].TrimEnd('/') -ne ($expectedRoot + '/runtime/ms-playwright')) {
        throw 'PLAYWRIGHT_BROWSERS_PATH must remain inside the invoked D-drive runtime.'
    }
    if ($result['TPS_DB_HOST'] -ne '127.0.0.1' -or $result['TPS_REDIS_HOST'] -ne '127.0.0.1') {
        throw 'Database and Redis hosts must be fixed to 127.0.0.1.'
    }
    if ($result['TPS_DB_NAME'] -cne 'people_search' -or $result['TPS_DB_USER'] -cne 'tps_app') {
        throw 'Database name and application user must match the fixed local MySQL deployment.'
    }
    if ($result['TPS_TIMEZONE'] -cne 'Asia/Shanghai') {
        throw 'TPS_TIMEZONE must match the reviewed local deployment.'
    }
    $dbPort = Convert-ToApprovedPort $result['TPS_DB_PORT'] 'TPS_DB_PORT'
    $redisPort = Convert-ToApprovedPort $result['TPS_REDIS_PORT'] 'TPS_REDIS_PORT'
    $dashboardPort = Convert-ToApprovedPort $result['TPS_DASHBOARD_PORT'] 'TPS_DASHBOARD_PORT'
    $ports = @($dbPort, $redisPort, $dashboardPort)
    if (@($ports | Select-Object -Unique).Count -ne 3) {
        throw 'Database, Redis, and dashboard ports must be distinct.'
    }
    return $result
}

function Set-TpsProcessEnvironment {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Configuration,
        [switch]$IncludeServiceCredentials
    )

    foreach ($overrideName in @(
        'PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'PYTHONINSPECT',
        'PYTHONBREAKPOINT', 'PYTHONUSERBASE', 'PYTHONEXECUTABLE',
        'NODE_OPTIONS', 'NODE_PATH',
        'PLAYWRIGHT_NODEJS_PATH', 'PLAYWRIGHT_DOWNLOAD_HOST',
        'PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST', 'PLAYWRIGHT_FIREFOX_DOWNLOAD_HOST',
        'PLAYWRIGHT_WEBKIT_DOWNLOAD_HOST', 'PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT',
        'PLAYWRIGHT_BROWSERS_PATH'
    )) {
        [Environment]::SetEnvironmentVariable($overrideName, $null, 'Process')
    }
    $serviceKeys = @(
        'TPS_DB_HOST', 'TPS_DB_PORT', 'TPS_DB_NAME', 'TPS_DB_USER', 'TPS_DB_PASSWORD',
        'TPS_REDIS_HOST', 'TPS_REDIS_PORT', 'TPS_REDIS_PASSWORD'
    )
    foreach ($name in $script:AllowedRuntimeKeys) {
        if ($name -eq 'TPS_MYSQL_ROOT_PASSWORD') {
            continue
        }
        if (-not $IncludeServiceCredentials -and $serviceKeys -contains $name) {
            [Environment]::SetEnvironmentVariable($name, $null, 'Process')
            continue
        }
        if ($Configuration.ContainsKey($name)) {
            [Environment]::SetEnvironmentVariable($name, [string]$Configuration[$name], 'Process')
        }
    }
    # The root credential is only introduced around schema initialization in
    # Start-Stack.ps1.  Do not leak it to pip, browser, dashboard, probe, or
    # shutdown child processes through generic environment setup.
    [Environment]::SetEnvironmentVariable('TPS_MYSQL_ROOT_PASSWORD', $null, 'Process')
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONNOUSERSITE = '1'
    $env:PYTHONSAFEPATH = '1'
    $env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = '1'
    if ($IncludeServiceCredentials) {
        $env:REDIS_HOST = $Configuration['TPS_REDIS_HOST']
        $env:REDIS_PORT = $Configuration['TPS_REDIS_PORT']
        $env:REDIS_PASSWORD = $Configuration['TPS_REDIS_PASSWORD']
    }
    else {
        [Environment]::SetEnvironmentVariable('REDIS_HOST', $null, 'Process')
        [Environment]::SetEnvironmentVariable('REDIS_PORT', $null, 'Process')
        [Environment]::SetEnvironmentVariable('REDIS_PASSWORD', $null, 'Process')
    }
    $runtimeTemp = Join-Path (Get-NormalizedInstallRoot $Configuration['TPS_INSTALL_ROOT']) 'runtime\temp'
    if (-not (Test-Path -LiteralPath $runtimeTemp -PathType Container)) {
        throw 'D-drive runtime temp directory is missing.'
    }
    $env:TEMP = $runtimeTemp
    $env:TMP = $runtimeTemp
}

function Clear-TpsServiceCredentialEnvironment {
    foreach ($name in @(
        'TPS_DB_PASSWORD', 'TPS_MYSQL_ROOT_PASSWORD', 'TPS_REDIS_PASSWORD',
        'REDIS_PASSWORD', 'TPS_RELEASE_LAUNCH_TOKEN'
    )) {
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
}

function Get-TpsPythonPath {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $candidate = Join-Path $InstallRoot 'runtime\.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "Approved offline Python environment is missing: $candidate"
    }
    return $candidate
}

function Get-TpsAppRoot {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $path = Join-Path $InstallRoot 'app'
    if (-not (Test-Path -LiteralPath (Join-Path $path 'tools\dashboard_api.py') -PathType Leaf)) {
        throw "Application bundle is incomplete under: $path"
    }
    Assert-NoReparsePoint $path
    return $path
}

function Get-TpsComposeArguments {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string]$AppRoot
    )
    $environmentPath = Join-Path $InstallRoot 'config\runtime.env'
    $composePath = Join-Path $AppRoot 'deploy\windows-full\docker-compose.yml'
    if (-not (Test-Path -LiteralPath $composePath -PathType Leaf)) {
        throw 'The pinned Windows full-stack compose definition is missing.'
    }
    return @('--context', 'desktop-linux', '--env-file', $environmentPath, '-f', $composePath)
}

function Get-TpsDockerTools {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    $root = Get-NormalizedInstallRoot $InstallRoot
    $programFiles = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
    if ([string]::IsNullOrWhiteSpace($programFiles)) {
        throw 'The protected Program Files directory could not be resolved.'
    }
    $resources = Join-Path $programFiles 'Docker\Docker\resources'
    $desktopPath = Join-Path $programFiles 'Docker\Docker\Docker Desktop.exe'
    $dockerPath = Join-Path $resources 'bin\docker.exe'
    $composePath = Join-Path $programFiles 'Docker\cli-plugins\docker-compose.exe'
    foreach ($path in @($desktopPath, $dockerPath, $composePath)) {
        $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if (-not ($item -is [IO.FileInfo]) -or
            (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
            throw "Docker Desktop tool is not a regular file: $path"
        }
        Assert-NoReparsePoint $path
    }

    $bundleManifest = Get-Content -LiteralPath (Join-Path $root 'bundle-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $installerRecord = @(
        $bundleManifest.files |
            Where-Object { [string]$_.path -eq 'vendor/docker/DockerDesktopInstaller.exe' }
    )
    if ($installerRecord.Count -ne 1 -or
        [string]$installerRecord[0].authenticode_thumbprint -notmatch '^[A-Fa-f0-9]{40}$') {
        throw 'The bundle manifest does not contain one approved Docker publisher identity.'
    }
    $componentVersions = $installerRecord[0].component_versions
    if ($null -eq $componentVersions) {
        throw 'The bundle manifest does not contain the approved Docker component versions.'
    }
    $requiredVersionKeys = @(
        'desktop_product_version', 'docker_cli_version', 'compose_version', 'engine_version'
    )
    $actualVersionKeys = @($componentVersions.PSObject.Properties.Name)
    if ($actualVersionKeys.Count -ne $requiredVersionKeys.Count) {
        throw 'The bundle manifest does not contain the exact approved Docker component versions.'
    }
    foreach ($name in $requiredVersionKeys) {
        $versionText = [string]$componentVersions.$name
        if ($actualVersionKeys -notcontains $name -or
            $versionText -notmatch '^\d+\.\d+\.\d+(?:\.\d+)?$') {
            throw "The bundle manifest has an invalid Docker component version: $name"
        }
    }
    $expectedSigner = ([string]$installerRecord[0].authenticode_thumbprint).ToUpperInvariant()
    foreach ($path in @($desktopPath, $dockerPath, $composePath)) {
        $signature = Get-AuthenticodeSignature -LiteralPath $path
        if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or
            $signature.SignerCertificate.Thumbprint.ToUpperInvariant() -ne $expectedSigner) {
            throw "Docker Desktop tool does not match the approved publisher: $path"
        }
    }
    $desktopVersion = ([string](Get-Item -LiteralPath $desktopPath -Force).VersionInfo.ProductVersion).Trim()
    if ($desktopVersion -cne [string]$componentVersions.desktop_product_version) {
        throw 'Installed Docker Desktop product version does not match the offline installer manifest.'
    }
    $dockerVersionOutput = ([string](& $dockerPath --version 2>$null)).Trim()
    $dockerVersionMatch = [regex]::Match($dockerVersionOutput, '^Docker version (\d+\.\d+\.\d+(?:\.\d+)?),')
    if ($LASTEXITCODE -ne 0 -or -not $dockerVersionMatch.Success -or
        $dockerVersionMatch.Groups[1].Value -cne [string]$componentVersions.docker_cli_version) {
        throw 'Installed Docker CLI version does not match the offline installer manifest.'
    }
    $composeVersion = ([string](& $composePath version --short 2>$null)).Trim().TrimStart('v')
    if ($LASTEXITCODE -ne 0 -or
        $composeVersion -cne [string]$componentVersions.compose_version) {
        throw 'Installed Docker Compose version does not match the offline installer manifest.'
    }
    return [pscustomobject][ordered]@{
        Desktop = [IO.Path]::GetFullPath($desktopPath)
        Docker = [IO.Path]::GetFullPath($dockerPath)
        Compose = [IO.Path]::GetFullPath($composePath)
        EngineVersion = [string]$componentVersions.engine_version
    }
}

function New-CryptographicHexSecret {
    param([int]$Bytes = 32)
    if ($Bytes -lt 16) {
        throw 'A runtime secret must contain at least 16 random bytes.'
    }
    $buffer = New-Object byte[] $Bytes
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($buffer)
    }
    finally {
        $generator.Dispose()
    }
    return (($buffer | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Protect-TpsSecretFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    $icacls = Get-TpsNativeSystemToolPath 'icacls.exe'
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if ($null -eq $identity.User) {
        throw 'Could not determine the current Windows user SID.'
    }
    $userGrant = '*' + $identity.User.Value + ':(F)'
    & $icacls $Path '/reset' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not reset stale permissions on protected file: $Path"
    }
    & $icacls $Path '/setowner' $identity.Name | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not set the approved owner on protected file: $Path"
    }
    & $icacls $Path '/inheritance:r' '/grant:r' $userGrant '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not apply the required ACL to protected file: $Path"
    }
}

function Assert-TpsProtectedFileAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$AllowApprovedInheritance
    )

    $acl = Get-Acl -LiteralPath $Path
    if (-not $AllowApprovedInheritance -and -not $acl.AreAccessRulesProtected) {
        throw "Protected runtime file inherits permissions: $Path"
    }
    $current = [Security.Principal.WindowsIdentity]::GetCurrent()
    if ($null -eq $current.User) {
        throw 'Could not determine the current Windows user SID.'
    }
    try {
        $ownerAccount = New-Object -TypeName Security.Principal.NTAccount -ArgumentList $acl.Owner
        $ownerSid = $ownerAccount.Translate([Security.Principal.SecurityIdentifier]).Value
    }
    catch {
        throw "Protected runtime file has an unresolvable owner: $Path"
    }
    if ($ownerSid -ne $current.User.Value) {
        throw "Protected runtime file has an unapproved owner: $Path"
    }
    $allowed = @{}
    $allowed[$current.User.Value] = $true
    $allowed['S-1-5-18'] = $true
    $allowed['S-1-5-32-544'] = $true
    $seen = @{}
    foreach ($rule in $acl.Access) {
        try {
            $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        }
        catch {
            throw "Protected runtime file has an unresolvable ACL entry: $Path"
        }
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            -not $allowed.ContainsKey($sid)) {
            throw "Protected runtime file grants access outside the approved local principals: $Path"
        }
        $seen[$sid] = $true
    }
    foreach ($sid in $allowed.Keys) {
        if (-not $seen.ContainsKey($sid)) {
            throw "Protected runtime file is missing a required ACL entry: $Path"
        }
    }
}

function Assert-TpsProtectedDirectoryAcl {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Protected runtime directory is missing: $Path"
    }
    Assert-NoReparsePoint $Path
    Assert-TpsProtectedFileAcl $Path -AllowApprovedInheritance
}

function Protect-TpsInstallTree {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    $icacls = Get-TpsNativeSystemToolPath 'icacls.exe'
    $root = Get-NormalizedInstallRoot $InstallRoot
    Assert-NoReparsePoint $root
    foreach ($item in Get-ChildItem -LiteralPath $root -Recurse -Force) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing to secure an install tree containing a reparse point: $($item.FullName)"
        }
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if ($null -eq $identity.User) {
        throw 'Could not determine the current Windows user SID.'
    }
    $userGrant = '*' + $identity.User.Value + ':(OI)(CI)(F)'
    & $icacls $root '/reset' '/T' '/C' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not reset stale permissions across D:\TruePeopleSearch.'
    }
    & $icacls $root '/setowner' $identity.Name '/T' '/C' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not set the approved owner across D:\TruePeopleSearch.'
    }
    & $icacls $root '/inheritance:r' '/grant:r' $userGrant '*S-1-5-18:(OI)(CI)(F)' '*S-1-5-32-544:(OI)(CI)(F)' '/T' '/C' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not protect the D:\TruePeopleSearch directory tree.'
    }
    Assert-TpsProtectedDirectoryAcl $root
}

function Get-TpsManagedRuntimeRecords {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    $root = Get-NormalizedInstallRoot $InstallRoot
    $rootPrefix = $root.TrimEnd('\') + '\'
    $managedTrees = @(
        'runtime\python',
        'runtime\.venv',
        'runtime\ms-playwright\chromium-1243',
        'runtime\ms-playwright\chromium_headless_shell-1243'
    )
    $records = New-Object 'System.Collections.Generic.List[object]'
    foreach ($relativeTree in $managedTrees) {
        $tree = Join-Path $root $relativeTree
        if (-not (Test-Path -LiteralPath $tree -PathType Container)) {
            throw "Managed runtime tree is missing: $relativeTree"
        }
        Assert-NoReparsePoint $tree
        foreach ($item in Get-ChildItem -LiteralPath $tree -Recurse -Force) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Managed runtime contains a reparse point: $($item.FullName)"
            }
            if ($item.PSIsContainer) {
                continue
            }
            if (-not ($item -is [IO.FileInfo])) {
                throw "Managed runtime entry is not a regular file: $($item.FullName)"
            }
            $relative = $item.FullName.Substring($rootPrefix.Length).Replace('\', '/')
            if (-not $relative -or $relative -match ':' -or $relative -match '(^|/)\.\.(/|$)') {
                throw "Managed runtime contains an unsafe path: $relative"
            }
            $records.Add([pscustomobject][ordered]@{
                path = $relative
                size = [Int64]$item.Length
                sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            })
        }
    }
    return @($records | Sort-Object -Property path)
}

function Write-TpsRuntimeManifest {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    $root = Get-NormalizedInstallRoot $InstallRoot
    $bundleManifestPath = Join-Path $root 'bundle-manifest.json'
    $bundleManifest = Get-Content -LiteralPath $bundleManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$bundleManifest.commit -notmatch '^[A-Fa-f0-9]{40}$') {
        throw 'Cannot bind the managed runtime to an invalid bundle commit.'
    }
    $bundleManifestSha256 = (Get-FileHash -LiteralPath $bundleManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $manifest = [ordered]@{
        schema_version = 2
        target = 'windows-amd64'
        source_commit = ([string]$bundleManifest.commit).ToLowerInvariant()
        bundle_manifest_sha256 = $bundleManifestSha256
        files = @(Get-TpsManagedRuntimeRecords $root)
    }
    if (@($manifest.files).Count -eq 0) {
        throw 'The managed runtime manifest would be empty.'
    }

    $manifestPath = Join-Path $root 'runtime\runtime-manifest.json'
    $temporaryPath = Join-Path (Split-Path -Parent $manifestPath) ('.runtime-manifest.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $json = ($manifest | ConvertTo-Json -Depth 5) + "`r`n"
        [IO.File]::WriteAllText($temporaryPath, $json, (New-Object Text.UTF8Encoding($false)))
        Protect-TpsSecretFile $temporaryPath
        Move-Item -LiteralPath $temporaryPath -Destination $manifestPath
        $temporaryPath = $null
    }
    finally {
        if ($temporaryPath -and (Test-Path -LiteralPath $temporaryPath)) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}

function Assert-TpsRuntimeManifest {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    $root = Get-NormalizedInstallRoot $InstallRoot
    $manifestPath = Join-Path $root 'runtime\runtime-manifest.json'
    $item = Get-Item -LiteralPath $manifestPath -Force -ErrorAction Stop
    if (-not ($item -is [IO.FileInfo]) -or
        (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) -or
        $item.Length -gt 16777216) {
        throw 'The managed runtime manifest is not a valid regular file.'
    }
    Assert-TpsProtectedFileAcl $manifestPath
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $bundleManifestPath = Join-Path $root 'bundle-manifest.json'
    $bundleManifest = Get-Content -LiteralPath $bundleManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $bundleManifestSha256 = (Get-FileHash -LiteralPath $bundleManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($manifest.schema_version -ne 2 -or $manifest.target -ne 'windows-amd64' -or
        [string]$manifest.source_commit -notmatch '^[A-Fa-f0-9]{40}$' -or
        ([string]$manifest.source_commit).ToLowerInvariant() -ne ([string]$bundleManifest.commit).ToLowerInvariant() -or
        [string]$manifest.bundle_manifest_sha256 -notmatch '^[A-Fa-f0-9]{64}$' -or
        ([string]$manifest.bundle_manifest_sha256).ToLowerInvariant() -ne $bundleManifestSha256) {
        throw 'The managed runtime manifest does not belong to this application bundle.'
    }

    $expected = @{}
    foreach ($record in @($manifest.files)) {
        $relative = ([string]$record.path).Replace('\', '/')
        if ($relative -notmatch '^runtime/(?:python|\.venv|ms-playwright/(?:chromium-1243|chromium_headless_shell-1243))/' -or
            $relative -match ':' -or $relative -match '(^|/)\.\.(/|$)' -or
            $expected.ContainsKey($relative) -or [string]$record.sha256 -notmatch '^[A-Fa-f0-9]{64}$') {
            throw 'The managed runtime manifest contains an unsafe or duplicate record.'
        }
        $expected[$relative] = $record
    }
    if ($expected.Count -eq 0) {
        throw 'The managed runtime manifest contains no file records.'
    }

    $actualRecords = @(Get-TpsManagedRuntimeRecords $root)
    if ($actualRecords.Count -ne $expected.Count) {
        throw 'The managed runtime file count changed after installation.'
    }
    foreach ($actual in $actualRecords) {
        if (-not $expected.ContainsKey($actual.path)) {
            throw "Unexpected managed runtime file: $($actual.path)"
        }
        $record = $expected[$actual.path]
        if ([Int64]$record.size -ne [Int64]$actual.size -or
            ([string]$record.sha256).ToLowerInvariant() -ne ([string]$actual.sha256).ToLowerInvariant()) {
            throw "Managed runtime checksum mismatch: $($actual.path)"
        }
    }
}

function Get-TpsVerifiedDockerImageIds {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    $root = Get-NormalizedInstallRoot $InstallRoot
    $dockerTools = Get-TpsDockerTools $root
    $docker = $dockerTools.Docker
    $bundleManifest = Get-Content -LiteralPath (Join-Path $root 'bundle-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $roleToTag = @{
        'mysql-image' = 'tps-offline/mysql:8.4.11-amd64'
        'redis-image' = 'tps-offline/redis:7.4.8-alpine-amd64'
    }
    $expected = @{}
    foreach ($record in @($bundleManifest.files)) {
        $role = [string]$record.role
        if (-not $roleToTag.ContainsKey($role)) {
            continue
        }
        $imageId = ([string]$record.image_id).ToLowerInvariant()
        if ($imageId -notmatch '^sha256:[a-f0-9]{64}$' -or $expected.ContainsKey($roleToTag[$role])) {
            throw "Bundle manifest has an invalid Docker image identity for role: $role"
        }
        $expected[$roleToTag[$role]] = $imageId
    }
    if ($expected.Count -ne $roleToTag.Count) {
        throw 'Bundle manifest is missing a pinned Docker image identity.'
    }

    foreach ($tag in $expected.Keys) {
        $actualId = ([string](& $docker --context desktop-linux image inspect $tag --format '{{.Id}}' 2>$null)).Trim().ToLowerInvariant()
        if ($LASTEXITCODE -ne 0 -or $actualId -ne $expected[$tag]) {
            throw "Docker image does not match the verified offline archive: $tag"
        }
        $architecture = ([string](& $docker --context desktop-linux image inspect $tag --format '{{.Architecture}}' 2>$null)).Trim().ToLowerInvariant()
        if ($LASTEXITCODE -ne 0 -or $architecture -ne 'amd64') {
            throw "Docker image has the wrong architecture: $tag"
        }
    }
    return $expected
}

function Assert-TpsDockerStorageOnD {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    $root = Get-NormalizedInstallRoot $InstallRoot
    $dockerData = Join-Path $root 'runtime\docker-data'
    if (-not (Test-Path -LiteralPath $dockerData -PathType Container)) {
        throw 'Docker Desktop WSL data root is missing from D:\TruePeopleSearch\runtime\docker-data.'
    }
    Assert-TpsProtectedDirectoryAcl $dockerData
    Assert-TpsStorageDirectoryTree $dockerData
    $dockerTools = Get-TpsDockerTools $root
    $docker = $dockerTools.Docker
    foreach ($overrideName in @('DOCKER_HOST', 'DOCKER_CONTEXT')) {
        if (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($overrideName, 'Process'))) {
            throw "$overrideName must be unset; remote or overridden Docker endpoints are not permitted."
        }
    }
    $context = ([string](& $docker context show 2>$null)).Trim()
    if ($LASTEXITCODE -ne 0 -or $context -ne 'desktop-linux') {
        throw 'The active Docker context must be the local Docker Desktop Linux engine (desktop-linux).'
    }
    $contextJson = (& $docker context inspect $context 2>$null | Out-String)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($contextJson)) {
        throw 'The active Docker context endpoint could not be inspected.'
    }
    try {
        $contextRecords = @($contextJson | ConvertFrom-Json)
    }
    catch {
        throw 'The active Docker context returned invalid endpoint metadata.'
    }
    if ($contextRecords.Count -ne 1 -or
        [string]$contextRecords[0].Endpoints.docker.Host -cne 'npipe:////./pipe/dockerDesktopLinuxEngine') {
        throw 'The active Docker endpoint is not the local Docker Desktop Linux named pipe.'
    }
    $engineVersion = ([string](& $docker --context desktop-linux version --format '{{.Server.Version}}' 2>$null)).Trim()
    if ($LASTEXITCODE -ne 0 -or $engineVersion -cne [string]$dockerTools.EngineVersion) {
        throw 'The active Docker engine version does not match the offline installer manifest.'
    }
    $distributionRoot = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss'
    $dockerDistributions = @()
    if (Test-Path -LiteralPath $distributionRoot) {
        $dockerDistributions = @(
            Get-ChildItem -LiteralPath $distributionRoot -ErrorAction Stop |
                ForEach-Object { Get-ItemProperty -LiteralPath $_.PSPath } |
                Where-Object { [string]$_.DistributionName -like 'docker-desktop*' }
        )
    }
    if ($dockerDistributions.Count -eq 0) {
        throw 'The active Docker Desktop WSL distribution registration could not be verified.'
    }
    $dataPrefix = [IO.Path]::GetFullPath($dockerData).TrimEnd('\') + '\'
    foreach ($distribution in $dockerDistributions) {
        $basePathText = [Environment]::ExpandEnvironmentVariables([string]$distribution.BasePath)
        if ($basePathText.StartsWith('\\?\')) {
            $basePathText = $basePathText.Substring(4)
        }
        $basePath = [IO.Path]::GetFullPath($basePathText)
        if (-not ($basePath.TrimEnd('\') + '\').StartsWith($dataPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Docker Desktop WSL distribution is not registered under the approved D-drive data root: $($distribution.DistributionName)"
        }
        if (-not (Test-Path -LiteralPath $basePath -PathType Container)) {
            throw "Docker Desktop WSL distribution path is missing: $($distribution.DistributionName)"
        }
        Assert-NoReparsePoint $basePath
    }
    $disks = @(Get-ChildItem -LiteralPath $dockerData -Recurse -Force -File -Filter '*.vhdx' -ErrorAction Stop)
    if ($disks.Count -eq 0 -or -not ($disks | Where-Object { $_.Length -gt 1048576 })) {
        throw 'Docker Desktop data disk was not verified under D:\TruePeopleSearch\runtime\docker-data.'
    }
    foreach ($disk in $disks) {
        Assert-NoReparsePoint $disk.FullName
    }
}

function Assert-TpsSupportedWindowsHost {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or
        -not [Environment]::Is64BitOperatingSystem -or
        $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
        throw 'This bundle supports only x64 Windows 10/11 hosts.'
    }
    $operatingSystem = Get-CimInstance -ClassName Win32_OperatingSystem
    if ([int]$operatingSystem.ProductType -ne 1) {
        throw 'Docker Desktop is not supported on Windows Server; use a supported Windows 10/11 client host.'
    }
    $build = [int]$operatingSystem.BuildNumber
    if (($build -ge 22000 -and $build -lt 22631) -or ($build -lt 22000 -and $build -lt 19045)) {
        throw 'Windows must be a serviced Windows 10 22H2 or Windows 11 23H2-or-newer build.'
    }
    $wslCandidates = @()
    $storeWsl = Join-Path ${env:ProgramFiles} 'WSL\wsl.exe'
    if (Test-Path -LiteralPath $storeWsl -PathType Leaf) {
        # The Store payload entry itself may be a link: do not apply the
        # bundle reparse-point rule to this Microsoft-owned tool. Trust here
        # comes from the version probe below, not from the path shape; only
        # administrators can plant files under Program Files anyway.
        $wslCandidates += [IO.Path]::GetFullPath($storeWsl)
    }
    # The System32 inbox copy may be an ancient stub that does not understand
    # '--version' at all; it is only a fallback when the Store app is absent.
    $wslCandidates += Get-TpsNativeSystemToolPath 'wsl.exe'
    $wsl = $null
    $wslOutput = ''
    foreach ($candidate in $wslCandidates) {
        $probe = (& $candidate --version 2>&1 | Out-String)
        if ($LASTEXITCODE -eq 0 -and [regex]::Match($probe, '\d+\.\d+\.\d+').Success) {
            $wsl = $candidate
            $wslOutput = $probe
            break
        }
    }
    if (-not $wsl) {
        throw 'WSL 2.1.5 or newer is required before installing Docker Desktop.'
    }
    $versionMatch = [regex]::Match($wslOutput, '\d+\.\d+\.\d+')
    if ([Version]$versionMatch.Value -lt [Version]'2.1.5') {
        throw 'WSL 2.1.5 or newer is required before installing Docker Desktop.'
    }
}

function Assert-TpsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object -TypeName Security.Principal.WindowsPrincipal -ArgumentList $identity
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'This host change must be run from an elevated PowerShell window.'
    }
}
