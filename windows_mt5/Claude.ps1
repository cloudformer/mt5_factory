$ErrorActionPreference="Stop"

Write-Host "=== Claude Code Installer ===" -ForegroundColor Cyan


function Refresh-Path {

    $machine =
    [Environment]::GetEnvironmentVariable(
        "Path",
        "Machine"
    )

    $user =
    [Environment]::GetEnvironmentVariable(
        "Path",
        "User"
    )

    $env:Path="$machine;$user"

}


Refresh-Path


# ==========================
# Node
# ==========================

Write-Host "=== Checking Node.js ===" -ForegroundColor Cyan


$nodeDirs=@(
"C:\Program Files\nodejs",
"C:\Program Files (x86)\nodejs"
)


$nodeDir=$null


foreach($d in $nodeDirs){

    if(Test-Path "$d\node.exe"){
        $nodeDir=$d
        break
    }

}


if(!$nodeDir){

    Write-Host "Installing Node.js..." -ForegroundColor Yellow


    $msi="$env:TEMP\node-lts.msi"


    Invoke-WebRequest `
    "https://nodejs.org/dist/v22.17.0/node-v22.17.0-x64.msi" `
    -OutFile $msi


    Start-Process `
    msiexec.exe `
    -ArgumentList "/i `"$msi`" /passive" `
    -Wait


    Start-Sleep 10


    foreach($d in $nodeDirs){

        if(Test-Path "$d\node.exe"){
            $nodeDir=$d
            break
        }

    }

}


if(!$nodeDir){

    throw "Node.js installation failed"

}


Write-Host "Node: $nodeDir" -ForegroundColor Green


$env:Path += ";$nodeDir"



& "$nodeDir\node.exe" --version



# ==========================
# npm
# ==========================

Write-Host "=== Checking npm ===" -ForegroundColor Cyan


$npm="$nodeDir\npm.cmd"


if(!(Test-Path $npm)){

    throw "npm.cmd missing"

}


& $npm --version



# ==========================
# Install Claude
# ==========================

Write-Host "=== Installing Claude Code ===" -ForegroundColor Cyan


& $npm install -g @anthropic-ai/claude-code



# ==========================
# Locate Claude
# ==========================

Write-Host "=== Searching Claude ===" -ForegroundColor Cyan


$prefix=& $npm prefix -g


$candidates=@(
"$prefix\claude.cmd",
"$prefix\node_modules\.bin\claude.cmd",
"$env:APPDATA\npm\claude.cmd"
)


$claude=$null


foreach($c in $candidates){

    if(Test-Path $c){

        $claude=$c
        break

    }

}



if(!$claude){

    throw "Claude command not found"

}



Write-Host "Claude found:"
Write-Host $claude -ForegroundColor Green



# 当前窗口立即可用

$env:Path += ";$(Split-Path $claude)"



& $claude --version



# 永久加入用户 PATH

$currentUserPath=
[Environment]::GetEnvironmentVariable(
"Path",
"User"
)


$claudeDir=
Split-Path $claude


if($currentUserPath -notlike "*$claudeDir*"){

    [Environment]::SetEnvironmentVariable(
    "Path",
    "$currentUserPath;$claudeDir",
    "User"
    )

}


Write-Host ""
Write-Host "=== Refresh PATH ===" -ForegroundColor Cyan


# 获取 Claude 所在目录
$claudeDir = Split-Path $claude


# 写入用户 PATH
$userPath = [Environment]::GetEnvironmentVariable(
    "Path",
    "User"
)


if ($userPath -notlike "*$claudeDir*") {

    [Environment]::SetEnvironmentVariable(
        "Path",
        "$userPath;$claudeDir",
        "User"
    )

    Write-Host "Added PATH: $claudeDir"

}


# 当前 PowerShell 立即生效
$env:Path += ";$claudeDir"


Write-Host ""
Write-Host "=== Claude Test ===" -ForegroundColor Cyan

& "$claudeDir\claude.cmd" --version


# ==========================
# Restart VS Code
# ==========================

$code = Get-Process Code -ErrorAction SilentlyContinue

if ($code) {

    Write-Host ""
    Write-Host "Restarting VS Code..." -ForegroundColor Yellow

    $code | Stop-Process -Force

    Start-Sleep -Seconds 3

    Start-Process code

}


Write-Host ""
Write-Host "=================================" -ForegroundColor Green
Write-Host " Claude Code Ready " -ForegroundColor Green
Write-Host " Open VS Code again and run: claude "
Write-Host "=================================" -ForegroundColor Green