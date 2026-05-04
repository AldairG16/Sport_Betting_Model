# Disparar manualmente cualquier workflow del repo via GitHub API.
# Uso: .\scripts\_trigger_workflow.ps1
#  - Te pide el PAT (no se guarda)
#  - Te pide qué workflow disparar (morning/evening/closing/weekly/pre_kickoff)
#  - Hace POST a /actions/workflows/<file>.yml/dispatches con body {"ref":"master"}

$ErrorActionPreference = "Continue"

Write-Host ""
Write-Host "=== TRIGGER MANUAL DE WORKFLOW ===" -ForegroundColor Cyan
Write-Host ""

# Pedir el token sin mostrarlo
$secureToken = Read-Host "Pega tu PAT (no se mostrara)" -AsSecureString
$token = [System.Net.NetworkCredential]::new("", $secureToken).Password

if ([string]::IsNullOrWhiteSpace($token)) {
    Write-Host "[ERROR] Token vacio. Saliendo." -ForegroundColor Red
    exit 1
}

# Pedir qué workflow
Write-Host ""
Write-Host "Workflows disponibles:" -ForegroundColor Yellow
Write-Host "  1) morning"
Write-Host "  2) evening"
Write-Host "  3) closing"
Write-Host "  4) weekly"
Write-Host "  5) pre_kickoff"
Write-Host ""
$choice = Read-Host "Cual? (1-5)"

$workflow = switch ($choice) {
    "1" { "morning.yml" }
    "2" { "evening.yml" }
    "3" { "closing.yml" }
    "4" { "weekly.yml" }
    "5" { "pre_kickoff.yml" }
    default {
        Write-Host "[ERROR] Opcion invalida." -ForegroundColor Red
        exit 1
    }
}

$repo = "AldairG16/Sport_Betting_Model"
$url  = "https://api.github.com/repos/$repo/actions/workflows/$workflow/dispatches"

$headers = @{
    "Authorization" = "Bearer $token"
    "Accept"        = "application/vnd.github.v3+json"
    "Content-Type"  = "application/json"
}
$body = @{ ref = "master" } | ConvertTo-Json -Compress

Write-Host ""
Write-Host "Disparando $workflow..." -ForegroundColor Yellow
try {
    $resp = Invoke-WebRequest -Uri $url -Headers $headers -Method Post -Body $body -ErrorAction Stop
    if ($resp.StatusCode -eq 204) {
        Write-Host "[OK] Workflow disparado (204 No Content)" -ForegroundColor Green
        Write-Host ""
        Write-Host "Ve a https://github.com/$repo/actions/workflows/$workflow" -ForegroundColor Cyan
        Write-Host "y deberias ver un nuevo run en segundos." -ForegroundColor Cyan
    } else {
        Write-Host "[!] Status inesperado: $($resp.StatusCode)" -ForegroundColor Yellow
    }
}
catch {
    Write-Host "[FAIL] Error al disparar." -ForegroundColor Red
    Write-Host "  Status: $($_.Exception.Response.StatusCode.value__)" -ForegroundColor Red
    Write-Host "  Body  : $($_.ErrorDetails.Message)" -ForegroundColor Red
    exit 1
}
