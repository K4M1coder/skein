param(
    [string]$Version = "1.50.0"
)

$ErrorActionPreference = "Stop"
$architecture = if ([Environment]::Is64BitOperatingSystem) { "amd64" } else { throw "Syft requires a 64-bit Windows installation." }
$asset = "syft_${Version}_windows_${architecture}.zip"
$releaseBase = "https://github.com/anchore/syft/releases/download/v${Version}"
$installRoot = Join-Path $env:LOCALAPPDATA "Skein\tools\syft\$Version"
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("skein-syft-" + [Guid]::NewGuid().ToString("N"))
$archive = Join-Path $temporaryRoot $asset
$checksums = Join-Path $temporaryRoot "checksums.txt"

try {
    New-Item -ItemType Directory -Path $temporaryRoot -Force | Out-Null
    Invoke-WebRequest "$releaseBase/syft_${Version}_checksums.txt" -OutFile $checksums
    Invoke-WebRequest "$releaseBase/$asset" -OutFile $archive
    $checksumLine = Get-Content $checksums | Where-Object { $_ -match "\s+$([Regex]::Escape($asset))$" } | Select-Object -First 1
    if (-not $checksumLine) { throw "The official checksum file does not contain $asset." }
    $expected = ($checksumLine -split "\s+")[0].ToLowerInvariant()
    $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw "Syft archive checksum mismatch. Expected $expected, received $actual." }
    New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $installRoot -Force
    $executable = Join-Path $installRoot "syft.exe"
    if (-not (Test-Path -LiteralPath $executable)) { throw "The Syft archive did not contain syft.exe." }
    & $executable version
    Write-Host "Syft $Version installed at $executable"
}
finally {
    Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
}
