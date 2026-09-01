# Deploys the OXS WhatsApp Bridge to Azure App Service (Linux, Python).
# Idempotent: safe to re-run for code updates (it just re-zips and re-deploys).
#
# Cost: F1 (Free) SKU = $0/month. Switch to B1 (~$13/mo) later if the cold
# starts bother you:  az appservice plan update -g rg-oxs-whatsapp -n plan-oxs-whatsapp --sku B1
#
# Run from the project folder:  .\deploy-azure.ps1

param(
    [string]$ResourceGroup   = "rg-oxs-whatsapp",
    [string]$Location        = "westeurope",
    [string]$PlanName        = "plan-oxs-whatsapp",
    [string]$AppName         = "",           # auto-generated (globally unique) if empty
    [string]$Sku             = "F1",
    # Guard against deploying into the wrong Azure profile. Pass -ExpectedAccount ""
    # to skip the check.
    [string]$ExpectedAccount = "shalomamar89@gmail.com"
)

$ErrorActionPreference = "Stop"

# az is a native command: $ErrorActionPreference does NOT stop on its failures,
# so every call goes through this wrapper (or checks $LASTEXITCODE itself).
function Invoke-Az {
    $output = az @args
    if ($LASTEXITCODE -ne 0) {
        throw "az $($args -join ' ') failed (exit $LASTEXITCODE)"
    }
    return $output
}

$account = az account show --query "user.name" -o tsv
if ($LASTEXITCODE -ne 0 -or -not $account) {
    throw "Not logged in to Azure - run 'az login' first."
}
if ($ExpectedAccount -and $account -ne $ExpectedAccount) {
    throw "Active Azure account is '$account', expected '$ExpectedAccount'. Run 'az login' with the right account, or pass -ExpectedAccount '' to override."
}
Write-Host "Deploying with Azure account: $account"

$rgExists = Invoke-Az group exists -n $ResourceGroup

if (-not $AppName) {
    # Reuse the existing web app in the RG if there is one, else generate a name.
    $existing = $null
    if ($rgExists -eq "true") {
        $existing = az webapp list -g $ResourceGroup --query "[0].name" -o tsv
        if ($LASTEXITCODE -ne 0) { $existing = $null }
    }
    if ($existing) { $AppName = $existing }
    else { $AppName = "oxs-whatsapp-" + (Get-Random -Minimum 10000 -Maximum 99999) }
}
Write-Host "App name: $AppName"

Invoke-Az group create -n $ResourceGroup -l $Location -o none | Out-Null

Invoke-Az appservice plan create -g $ResourceGroup -n $PlanName --sku $Sku --is-linux -o none | Out-Null

$appCount = Invoke-Az webapp list -g $ResourceGroup --query "[?name=='$AppName'] | length(@)" -o tsv
if ($appCount -eq "0") {
    Invoke-Az webapp create -g $ResourceGroup -p $PlanName -n $AppName --runtime "PYTHON:3.12" -o none | Out-Null
}

Invoke-Az webapp update -g $ResourceGroup -n $AppName --https-only true -o none | Out-Null

# App Service Linux/Python expects the app on port 8000. FTP off while we're here.
Invoke-Az webapp config set -g $ResourceGroup -n $AppName --ftps-state Disabled `
    --startup-file "python -m uvicorn main:app --host 0.0.0.0 --port 8000" -o none | Out-Null

# Disable basic-auth publishing endpoints (scm + ftp) - zip deploy below uses
# AAD tokens and keeps working; this closes a standard credential-stuffing target.
Invoke-Az resource update -g $ResourceGroup --namespace Microsoft.Web `
    --resource-type basicPublishingCredentialsPolicies --parent "sites/$AppName" `
    -n scm --set properties.allow=false -o none | Out-Null
Invoke-Az resource update -g $ResourceGroup --namespace Microsoft.Web `
    --resource-type basicPublishingCredentialsPolicies --parent "sites/$AppName" `
    -n ftp --set properties.allow=false -o none | Out-Null

# Enable container logging so 'az webapp log tail' works from day one.
Invoke-Az webapp log config -g $ResourceGroup -n $AppName --docker-container-logging filesystem -o none | Out-Null

# Seed settings - placeholders for the secrets, real keys pasted in later (README).
# Existing values are always preserved on re-runs; when nothing is missing we
# skip the call entirely (appsettings set restarts the app).
$current = Invoke-Az webapp config appsettings list -g $ResourceGroup -n $AppName -o json | ConvertFrom-Json
$names = @($current | ForEach-Object { $_.name })

$toSet = @()
if ($names -notcontains "SCM_DO_BUILD_DURING_DEPLOYMENT") { $toSet += "SCM_DO_BUILD_DURING_DEPLOYMENT=true" }
if ($names -notcontains "OXS_GENERAL_API_KEY")       { $toSet += "OXS_GENERAL_API_KEY=CHANGE_ME" }
if ($names -notcontains "OXS_SERVICE_CALLS_API_KEY") { $toSet += "OXS_SERVICE_CALLS_API_KEY=CHANGE_ME" }
if ($names -notcontains "META_APP_SECRET")           { $toSet += "META_APP_SECRET=CHANGE_ME" }
if ($names -notcontains "META_VERIFY_TOKEN") {
    $token = -join ((48..57) + (97..122) | Get-Random -Count 32 | ForEach-Object {[char]$_})
    $toSet += "META_VERIFY_TOKEN=$token"
}
if ($toSet.Count -gt 0) {
    az webapp config appsettings set -g $ResourceGroup -n $AppName --settings $toSet -o none
    if ($LASTEXITCODE -ne 0) { throw "az webapp config appsettings set failed" }
}

# Package only the app files (never .env) and deploy via config-zip, the
# reliable Oryx-build path. One retry: the appsettings restart above can leave
# the SCM container briefly unavailable.
$zip = Join-Path $PSScriptRoot "app.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
$files = @("main.py", "oxs_service.py", "models.py", "phone_utils.py", "config.py", "requirements.txt") |
    ForEach-Object { Join-Path $PSScriptRoot $_ }
Compress-Archive -Path $files -DestinationPath $zip

az webapp deployment source config-zip -g $ResourceGroup -n $AppName --src $zip
if ($LASTEXITCODE -ne 0) {
    Write-Host "Deploy failed (SCM may be restarting) - retrying once in 30s..."
    Start-Sleep -Seconds 30
    az webapp deployment source config-zip -g $ResourceGroup -n $AppName --src $zip
    if ($LASTEXITCODE -ne 0) { throw "zip deploy failed twice - check 'az webapp log deployment show -g $ResourceGroup -n $AppName'" }
}

$verifyToken = Invoke-Az webapp config appsettings list -g $ResourceGroup -n $AppName `
    --query "[?name=='META_VERIFY_TOKEN'].value | [0]" -o tsv

Write-Host ""
Write-Host "=========================================================="
Write-Host " Deployed."
Write-Host " Health check : https://$AppName.azurewebsites.net/health"
Write-Host " Webhook URL  : https://$AppName.azurewebsites.net/webhook"
Write-Host " Verify token : $verifyToken"
Write-Host ""
Write-Host " Next steps:"
Write-Host " 1. Paste the two OXS keys + Meta app secret (README:"
Write-Host "    'Entering the API keys')."
Write-Host " 2. Open the health URL and WAIT until it returns JSON"
Write-Host "    (free tier cold start can take 1-2 min), then configure"
Write-Host "    the Meta webhook."
Write-Host "=========================================================="
