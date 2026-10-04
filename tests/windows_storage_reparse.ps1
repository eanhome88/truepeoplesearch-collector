# Run only on a disposable Windows test host, never on a customer installation.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\deploy\windows-full\Common.ps1')

# CPython on Windows does not expose AF_UNIX. Create the real socket through
# Winsock so the test exercises the native NTFS tag under PowerShell 5.1.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class TpsUnixSocketFixture {
    [DllImport("Ws2_32.dll")] static extern int WSAStartup(ushort version, byte[] data);
    [DllImport("Ws2_32.dll")] static extern int WSACleanup();
    [DllImport("Ws2_32.dll")] static extern IntPtr socket(int family, int type, int protocol);
    [DllImport("Ws2_32.dll")] static extern int bind(IntPtr handle, byte[] address, int length);
    [DllImport("Ws2_32.dll")] static extern int closesocket(IntPtr handle);
    [DllImport("Ws2_32.dll")] static extern int WSAGetLastError();
    public static void Create(string path) {
        byte[] name = Encoding.UTF8.GetBytes(path);
        if (name.Length >= 108) throw new ArgumentException("Socket fixture path is too long.");
        if (WSAStartup(0x0202, new byte[512]) != 0) throw new Exception("WSAStartup failed.");
        IntPtr handle = new IntPtr(-1);
        try {
            handle = socket(1, 1, 0);
            if (handle == new IntPtr(-1)) throw new Exception("socket failed: " + WSAGetLastError());
            byte[] address = new byte[110];
            address[0] = 1;
            Buffer.BlockCopy(name, 0, address, 2, name.Length);
            if (bind(handle, address, address.Length) != 0) throw new Exception("bind failed: " + WSAGetLastError());
        } finally {
            if (handle != new IntPtr(-1)) closesocket(handle);
            WSACleanup();
        }
    }
}
'@

$fixture = 'D:\TruePeopleSearch'
if (Test-Path -LiteralPath $fixture) {
    throw 'Refusing to overwrite an existing installation for this test.'
}
$mysql = Join-Path $fixture 'data\mysql'
$redis = Join-Path $fixture 'data\redis'
$passed = 0

function Assert-TestEqual {
    param($Actual, $Expected, [string]$Name)
    if ($Actual -ne $Expected) { throw "FAILED: $Name" }
    $script:passed++
}

function Assert-TestRejected {
    param([string]$Path, [string]$Name)
    $rejected = $false
    try { Assert-TpsStorageDirectoryTree $Path } catch { $rejected = $true }
    Assert-TestEqual $rejected $true $Name
}

function New-TestUnixSocket {
    param([string]$Path)
    [TpsUnixSocketFixture]::Create($Path)
}

try {
    New-Item -ItemType Directory -Path $mysql, $redis -Force | Out-Null
    $unixTag = [Convert]::ToUInt32('80000023', 16)
    Assert-TestEqual (ConvertFrom-TpsFsutilReparseTag @('Reparse Tag Value : 0x80000023', 'payload')) $unixTag 'native header'
    Assert-TestEqual (ConvertFrom-TpsFsutilReparseTag @('', 'Localized label : 0x80000023')) $unixTag 'localized header'
    Assert-TestEqual (ConvertFrom-TpsFsutilReparseTag @('Reparse Tag Value : 0xA000000C', '0x80000023')) ([Convert]::ToUInt32('A000000C', 16)) 'payload cannot replace tag'
    foreach ($bad in @('0x80000023', 'Error : 0x80000023 trailing', 'Tag : 0x23')) {
        $rejected = $false
        try { ConvertFrom-TpsFsutilReparseTag @($bad) | Out-Null } catch { $rejected = $true }
        Assert-TestEqual $rejected $true 'malformed header'
    }
    $lxDump = @(
        'Reparse Tag Value : 0xa000001d',
        '0000:  02 00 00 00 2f 74 6d 70  2f 6d 79 73 71 6c 2e 73  ..../tmp/mysql.s',
        '0010:  6f 63 6b                                          ock'
    )
    Assert-TestEqual (ConvertFrom-TpsLxSymlinkTarget $lxDump) '/tmp/mysql.sock' 'docker symlink target'
    $lxNul = @('0000: 02 00 00 00 2f 74 6d 70 2f 6d 79 73 71 6c 2e 73', '0010: 6f 63 6b 00')
    Assert-TestEqual (ConvertFrom-TpsLxSymlinkTarget $lxNul) '/tmp/mysql.sock' 'trailing NUL stripped'
    foreach ($badDump in @(
        @('0000: 02 00 00 00 2f 65 74 63 2f 70 61 73 73 77 64'),
        @('0000: 02 00 00 00 2f 74 6d 70 2f 6d 79 73 71 6c 2e 73', '0010: 6f 63 6b 2f 65 76 69 6c'),
        @('Print Name: /tmp/mysql.sock')
    )) {
        $rejected = $false
        try { ConvertFrom-TpsLxSymlinkTarget $badDump | Out-Null } catch { $rejected = $true }
        Assert-TestEqual $rejected $true 'unexpected symlink target'
    }

    $socket = Join-Path $mysql 'mysql.sock'
    New-TestUnixSocket $socket
    Assert-TestEqual (Get-TpsReparseTag $socket) $unixTag 'real native AF_UNIX tag'
    Assert-TpsStorageDirectoryTree $mysql
    $passed++
    Assert-TestEqual (Test-TpsMySqlUnixSocket $mysql (Get-Item -LiteralPath $socket -Force)) $true 'exact socket accepted'

    $other = Join-Path $mysql 'other.sock'
    New-TestUnixSocket $other
    Assert-TestRejected $mysql 'other socket name rejected'
    Remove-Item -LiteralPath $other -Force
    $nested = Join-Path $mysql 'nested'
    New-Item -ItemType Directory -Path $nested | Out-Null
    New-TestUnixSocket (Join-Path $nested 'mysql.sock')
    Assert-TestRejected $mysql 'nested socket rejected'
    Remove-Item -LiteralPath $nested -Recurse -Force
    New-TestUnixSocket (Join-Path $redis 'mysql.sock')
    Assert-TestRejected $redis 'Redis socket rejected'

    Remove-Item -LiteralPath $socket -Force
    $target = Join-Path $fixture 'outside.txt'
    [IO.File]::WriteAllText($target, '')
    New-Item -ItemType SymbolicLink -Path $socket -Target $target | Out-Null
    Assert-TestRejected $mysql 'zero-byte symlink rejected'
    Remove-Item -LiteralPath $socket -Force
    New-Item -ItemType Junction -Path $socket -Target $redis | Out-Null
    Assert-TestRejected $mysql 'junction rejected'
    # Remove only the junction itself, without traversing its target.
    [IO.Directory]::Delete($socket)

    New-TestUnixSocket $socket
    $originalQuery = ${function:Get-TpsReparseTag}
    try {
        function Get-TpsReparseTag { param([string]$Path) throw 'simulated query failure' }
        Assert-TestRejected $mysql 'query failure rejected'
    } finally { Set-Item Function:Get-TpsReparseTag $originalQuery }
    # Execute the real reader from the runbook using measured and missing data.
    $ops = [IO.File]::ReadAllText((Join-Path $PSScriptRoot '..\docs\ops\deploy-11c746b.md'))
    $reader = [regex]::Match($ops, '(?s)function Get-SuccessTotal \{.*?\r?\n\}\r?\n')
    if (-not $reader.Success) { throw 'Runbook metrics reader is missing.' }
    . ([scriptblock]::Create($reader.Value))
    $root = $fixture
    New-Item -ItemType Directory -Path (Join-Path $root 'app\scripts') -Force | Out-Null
    $script:statsFixture = '[STATS] metrics {"counters":{"success":1234},"success_rate_pct":75}'
    $script:statsExitFixture = 0
    $venvPython = { $global:LASTEXITCODE = $script:statsExitFixture; $script:statsFixture }
    Assert-TestEqual (Get-SuccessTotal) 1234 'counter is not success percentage'
    $script:statsFixture = '[STATS] metrics {"success_rate_pct":75}'
    Assert-TestEqual (Get-SuccessTotal) -1 'missing counter is not zero'
    $script:statsFixture = '[STATS] metrics {"counters":{"success":-2}}'
    Assert-TestEqual (Get-SuccessTotal) -1 'negative counter rejected'
    $script:statsExitFixture = 1
    Assert-TestEqual (Get-SuccessTotal) -1 'failed metrics command rejected'
    foreach ($relative in @('deploy-11c746b.md', 'deploy-mysql-socket-11f7d02.md')) {
        $text = [IO.File]::ReadAllText((Join-Path $PSScriptRoot ('..\docs\ops\' + $relative)))
        $blocks = [regex]::Matches($text, '(?s)```powershell\r?\n(.*?)```')
        foreach ($block in $blocks) {
            $tokens = $null
            $parseErrors = $null
            $null = [System.Management.Automation.Language.Parser]::ParseInput($block.Groups[1].Value, [ref]$tokens, [ref]$parseErrors)
            if ($parseErrors.Count -gt 0) { throw "Runbook parse errors in ${relative}: $parseErrors" }
            $passed++
        }
    }
    # Clear the deliberately simulated native failure after all assertions.
    $global:LASTEXITCODE = 0
    Write-Host "PASS windows-storage-reparse cases=$passed PS=$($PSVersionTable.PSVersion)"
} finally {
    # This root did not exist before this test; only this disposable fixture is removed.
    if (Test-Path -LiteralPath $fixture) { Remove-Item -LiteralPath $fixture -Recurse -Force }
}
