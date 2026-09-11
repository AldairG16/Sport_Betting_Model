# ============================================================
# build_msi.ps1 — Empaqueta el Dashboard como instalador MSI
# ============================================================
# Requisitos (una sola vez):
#   1. Python del proyecto con dependencias:  pip install -r requirements.txt pyinstaller
#   2. WiX Toolset 5 (gratis, de Microsoft):
#        winget install WiXToolset.WiXToolset
#      (o descarga de https://wixtoolset.org — añade a PATH)
#
# Uso (desde la raíz del proyecto, en PowerShell):
#   powershell -ExecutionPolicy Bypass -File installer\build_msi.ps1
#
# Produce:
#   installer\dist\BettingDashboard.msi   → instalador con .exe + .env.example
#   installer\dist\BettingDashboard.exe   → ejecutable portable (sin instalar)
# ============================================================

$ErrorActionPreference = "Stop"
$root  = Split-Path -Parent $PSScriptRoot
$dist  = Join-Path $PSScriptRoot "dist"

Write-Host "==> 1/3 Compilando ejecutable (PyInstaller)..." -ForegroundColor Cyan
Push-Location $root
python -m PyInstaller --noconfirm --onefile --name BettingDashboard `
    --distpath installer/dist --workpath installer/build `
    --specpath installer dashboard/entry.py
Pop-Location
if (-not (Test-Path "$dist\BettingDashboard.exe")) { throw "PyInstaller no produjo el exe" }

Write-Host "==> 2/3 Generando definicion WiX..." -ForegroundColor Cyan
$version = "1.0.0"
$guid    = "7C0A5B1E-4D2A-4F8B-9E3C-$(('{0:X12}' -f (Get-Random -Maximum 281474976710655)))"
$wxs = @"
<?xml version="1.0" encoding="UTF-8"?>
<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">
  <Package Name="Betting Dashboard" Manufacturer="Sport Betting Model"
           Version="$version" UpgradeCode="$guid" Language="1033"
           Compressed="yes" Scope="perMachine">
    <MajorUpgrade DowngradeErrorMessage="Una version mas nueva ya esta instalada." />
    <Media Id="1" Cabinet="media1.cab" EmbedCab="yes" />

    <StandardDirectory Id="ProgramFiles64Folder">
      <Directory Id="INSTALLDIR" Name="Betting Dashboard">
        <Component Id="MainExe">
          <File Id="Exe" Source="$dist\BettingDashboard.exe" />
        </Component>
        <Component Id="EnvExample">
          <File Id="Env" Source="$root\.env.example" Name=".env.example" />
        </Component>
      </Directory>
    </StandardDirectory>

    <!-- Icono en menu inicio -->
    <StandardDirectory Id="ProgramMenuFolder">
      <Directory Id="AppMenuDir" Name="Betting Dashboard">
        <Component Id="StartMenuShortcut">
          <Shortcut Id="AppShortcut" Name="Betting Dashboard"
                    Target="[INSTALLDIR]BettingDashboard.exe" WorkingDirectory="INSTALLDIR" />
          <RemoveFolder Id="RemoveAppMenu" On="uninstall" />
          <RegistryValue Root="HKCU" Key="Software\SportBettingModel\Dashboard"
                         Name="installed" Type="integer" Value="1" KeyPath="yes" />
        </Component>
      </Directory>
    </StandardDirectory>

    <Feature Id="Main" Title="Betting Dashboard" Level="1">
      <ComponentRef Id="MainExe" />
      <ComponentRef Id="EnvExample" />
      <ComponentRef Id="StartMenuShortcut" />
    </Feature>
  </Package>
</Wix>
"@
$wxsPath = Join-Path $PSScriptRoot "dashboard.wxs"
$wxs | Out-File -FilePath $wxsPath -Encoding utf8

Write-Host "==> 3/3 Compilando MSI (WiX)..." -ForegroundColor Cyan
$wix = Get-Command wix -ErrorAction SilentlyContinue
if (-not $wix) { throw "WiX no encontrado. Instala: winget install WiXToolset.WiXToolset (y reabre la terminal)" }
& wix build -o "$dist\BettingDashboard.msi" $wxsPath
if ($LASTEXITCODE -ne 0) { throw "wix build fallo (codigo $LASTEXITCODE)" }

Write-Host ""
Write-Host "LISTO:" -ForegroundColor Green
Write-Host "  $dist\BettingDashboard.msi   (instalador)"
Write-Host "  $dist\BettingDashboard.exe   (portable)"
Write-Host ""
Write-Host "IMPORTANTE tras instalar:" -ForegroundColor Yellow
Write-Host "  Copia .env.example como .env en la carpeta de instalacion"
Write-Host "  y pon ahi tu DB_URL de Neon. Sin eso el dashboard no arranca."
