# Test progresivo del GitHub PAT para cron-job.org
# Uso:
#   1. Pega tu PAT cuando lo pida (NO se guarda, solo en memoria de PowerShell)
#   2. El script corre 3 tests y te dice donde falla

$ErrorActionPreference = "Continue"

Write-Host ""
Write-Host "=== TEST PROGRESIVO DEL GITHUB PAT ===" -ForegroundColor Cyan
Write-Host ""

# Pedir el token sin mostrarlo en pantalla
$secureToken = Read-Host "Pega tu PAT (no se mostrara en pantalla)" -AsSecureString
$token = [System.Net.NetworkCredential]::new("", $secureToken).Password

if ([string]::IsNullOrWhiteSpace($token)) {
    Write-Host "[ERROR] Token vacio. Saliendo." -ForegroundColor Red
    exit 1
}

# Headers comunes
$headers = @{
    "Authorization" = "Bearer $token"
    "Accept"        = "application/vnd.github.v3+json"
    "User-Agent"    = "test-pat-script"
}

$repo = "AldairG16/Sport_Betting_Model"

# ─────────────────────────────────────────────────────────────
# TEST 1: el token es valido (endpoint publico /user)
# ─────────────────────────────────────────────────────────────
Write-Host "[TEST 1/4] Verificando que el token es valido..." -ForegroundColor Yellow
try {
    $resp = Invoke-RestMethod -Uri "https://api.github.com/user" `
                              -Headers $headers `
                              -Method Get `
                              -ErrorAction Stop
    Write-Host "[OK] Token valido. Usuario: $($resp.login)" -ForegroundColor Green
}
catch {
    Write-Host "[FAIL] Test 1 fallo." -ForegroundColor Red
    Write-Host "  Status: $($_.Exception.Response.StatusCode.value__)" -ForegroundColor Red
    Write-Host "  Body  : $($_.ErrorDetails.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host "DIAGNOSTICO: el token NO es valido. Posibles causas:" -ForegroundColor Yellow
    Write-Host "  - Pegaste mal (espacios al inicio/fin, chars cortados)"
    Write-Host "  - El token fue revocado"
    Write-Host "  - Esperaste menos de 60 seg desde que lo creaste"
    exit 1
}

# ─────────────────────────────────────────────────────────────
# TEST 2A: el token puede ver el repo (endpoint basico /repos/X)
# ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[TEST 2A/4] Verificando acceso BASICO al repo $repo (sin Actions)..." -ForegroundColor Yellow
try {
    $resp = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo" `
                              -Headers $headers `
                              -Method Get `
                              -ErrorAction Stop
    Write-Host "[OK] Repo visible. Privado: $($resp.private)  Default branch: $($resp.default_branch)" -ForegroundColor Green
    Write-Host "       Owner type: $($resp.owner.type)"
}
catch {
    Write-Host "[FAIL] Test 2A fallo." -ForegroundColor Red
    Write-Host "  Status: $($_.Exception.Response.StatusCode.value__)" -ForegroundColor Red
    Write-Host "  Body  : $($_.ErrorDetails.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host "DIAGNOSTICO: el token no ve el repo en absoluto. Causas:" -ForegroundColor Yellow
    Write-Host "  - El token NO es el que se muestra en la pantalla de GitHub"
    Write-Host "  - El token fue creado por otro usuario que no tiene acceso al repo"
    Write-Host "  - El repo esta archivado o eliminado"
    exit 1
}

# ─────────────────────────────────────────────────────────────
# TEST 2B: el token puede leer Actions (endpoint /repos/.../actions/workflows)
# ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[TEST 2B/4] Verificando acceso a ACTIONS del repo..." -ForegroundColor Yellow
try {
    $resp = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/actions/workflows" `
                              -Headers $headers `
                              -Method Get `
                              -ErrorAction Stop
    Write-Host "[OK] Acceso a Actions OK. Workflows visibles: $($resp.total_count)" -ForegroundColor Green
    foreach ($wf in $resp.workflows) {
        Write-Host "       - $($wf.name) [$($wf.path)] state=$($wf.state)"
    }
}
catch {
    Write-Host "[FAIL] Test 2B fallo." -ForegroundColor Red
    Write-Host "  Status: $($_.Exception.Response.StatusCode.value__)" -ForegroundColor Red
    Write-Host "  Body  : $($_.ErrorDetails.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host "DIAGNOSTICO: ve el repo pero NO Actions API. Causas:" -ForegroundColor Yellow
    Write-Host "  - Falta scope 'workflow' en el Classic PAT"
    Write-Host "  - Hay restricciones de organizacion sobre Actions"
    Write-Host "  - Token desactualizado: regenera y espera 60 seg"
    exit 1
}

# ─────────────────────────────────────────────────────────────
# TEST 3: el token puede DISPARAR workflows (endpoint /actions/workflows/.../dispatches)
# ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[TEST 3/4] Disparando workflow pre_kickoff.yml..." -ForegroundColor Yellow

$body = @{ ref = "master" } | ConvertTo-Json -Compress
$dispatchHeaders = $headers + @{ "Content-Type" = "application/json" }

try {
    $resp = Invoke-WebRequest -Uri "https://api.github.com/repos/$repo/actions/workflows/pre_kickoff.yml/dispatches" `
                              -Headers $dispatchHeaders `
                              -Method Post `
                              -Body $body `
                              -ErrorAction Stop
    Write-Host "[OK] Workflow disparado. Status: $($resp.StatusCode) (esperamos 204 No Content)" -ForegroundColor Green
    Write-Host ""
    Write-Host "Ve a https://github.com/$repo/actions/workflows/pre_kickoff.yml" -ForegroundColor Cyan
    Write-Host "y deberias ver un nuevo run iniciandose en segundos." -ForegroundColor Cyan
}
catch {
    Write-Host "[FAIL] Test 3/4 fallo." -ForegroundColor Red
    Write-Host "  Status: $($_.Exception.Response.StatusCode.value__)" -ForegroundColor Red
    Write-Host "  Body  : $($_.ErrorDetails.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host "DIAGNOSTICO: token tiene acceso de lectura pero no de ESCRITURA." -ForegroundColor Yellow
    Write-Host "  - Para Classic PAT: necesita scope 'workflow' (no solo 'repo')"
    Write-Host "  - Para Fine-grained: necesita 'Actions: Read and write'"
    exit 1
}

Write-Host ""
Write-Host "=================================================" -ForegroundColor Green
Write-Host "TODOS LOS TESTS PASARON" -ForegroundColor Green
Write-Host "=================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Tu PAT esta listo para cron-job.org. Configura asi:" -ForegroundColor Cyan
Write-Host ""
Write-Host "  URL    : https://api.github.com/repos/$repo/actions/workflows/pre_kickoff.yml/dispatches"
Write-Host "  Method : POST"
Write-Host "  Headers:"
Write-Host "    Authorization        = Bearer <tu-PAT>"
Write-Host "    Accept               = application/vnd.github.v3+json"
Write-Host "    Content-Type         = application/json"
Write-Host '  Body   : {"ref":"master"}'
Write-Host ""
