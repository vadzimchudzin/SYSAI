# SYSAI: установка агента на ПК сотрудника (docs/opo/TASKS.md, ОПО-6.2).
# Запуск — строка со страницы агента в SYSAI, в PowerShell (обычном, не от администратора):
#   $env:SYSAI_CODE='КОД'; irm __SYSAI_SERVER__/agents/api/install.ps1 | iex
# Ставит uv, синхронизатор SYSAI и Hermes (чего нет), создаёт профиль Hermes агента без встроенных навыков,
# применяет доступы, характер (SOUL.md) и навыки SYSAI_Hub, ставит окно агента «Агент SYSAI» (ярлык на рабочем
# столе), включает синхронизацию при входе в Windows.
# Повторный запуск с новым кодом обновляет то же самое. Совместим с Windows PowerShell 5.1.
& {
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$Server = '__SYSAI_SERVER__'
$Wheel = '__SYSAI_WHEEL__'
$Version = '__SYSAI_VERSION__'
$SysaiHome = Join-Path $env:LOCALAPPDATA 'SYSAI'

function Say([string]$Text) { Write-Host "SYSAI: $Text" -ForegroundColor Cyan }
function Warn([string]$Text) { Write-Host "SYSAI: $Text" -ForegroundColor Yellow }
function Stop-Install([string]$Text) { Write-Host "SYSAI: $Text" -ForegroundColor Red; throw 'SYSAI-STOP' }
function Test-Command([string]$Name) { [bool](Get-Command $Name -ErrorAction SilentlyContinue) }
function Update-PathNow {
    $parts = @([Environment]::GetEnvironmentVariable('Path', 'Machine'), [Environment]::GetEnvironmentVariable('Path', 'User'),
               (Join-Path $env:USERPROFILE '.local\bin'))
    $env:Path = ($parts | Where-Object { $_ }) -join ';'
}
function Stop-Sync {
    # the running synchronizer holds its files: asked to stop, then (if it did not) stopped by force
    $lock = Join-Path $SysaiHome 'run.lock'
    if (Test-Path $lock) {
        Set-Content -Path (Join-Path $SysaiHome 'stop') -Value 'stop' -ErrorAction SilentlyContinue
        for ($i = 0; ($i -lt 60) -and (Test-Path $lock); $i++) { Start-Sleep -Seconds 1 }
    }
    & taskkill /F /T /IM sysai-agentw.exe *> $null
    & taskkill /F /T /IM sysai-agent.exe *> $null
}

try {
    if ($env:OS -ne 'Windows_NT') { Stop-Install 'установщик — для Windows' }
    $Code = $env:SYSAI_CODE
    Remove-Item Env:SYSAI_CODE -ErrorAction SilentlyContinue
    if (-not $Code) { $Code = Read-Host 'Код подключения со страницы агента в SYSAI' }
    $Code = "$Code".Trim()
    if (-not $Code) { Stop-Install 'нет кода подключения: получите его на странице агента в SYSAI' }
    Say "сервер $Server, синхронизатор $Version"

    # 1. uv: ставит Python и синхронизатор, ничего не просит у администратора
    Update-PathNow
    if (-not (Test-Command 'uv')) {
        Say 'ставлю uv (Astral)...'
        & powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
        Update-PathNow
        if (-not (Test-Command 'uv')) { Stop-Install 'uv не установился. Поставьте его по https://docs.astral.sh/uv/ и запустите установщик снова' }
    }

    # 2. синхронизатор SYSAI
    Say 'ставлю синхронизатор SYSAI...'
    Stop-Sync
    & uv tool install --force --python 3.12 $Wheel
    if ($LASTEXITCODE -ne 0) { Stop-Install 'синхронизатор не установился (uv tool install). Закройте Hermes, если он открыт, и запустите установщик снова' }
    & uv tool update-shell *> $null
    Update-PathNow
    if (-not (Test-Command 'sysai-agent')) { Stop-Install 'после установки не найдена команда sysai-agent: откройте новое окно PowerShell и запустите установщик снова' }

    # 3. код подключения → токен агента (хранилище Windows), ключ подписи доступов
    Say 'подключаю компьютер к SYSAI...'
    & sysai-agent connect --server $Server --code $Code
    if ($LASTEXITCODE -ne 0) { Stop-Install 'код не принят. Получите новый на странице агента и запустите установщик снова' }
    $Info = (& sysai-agent status --json | Out-String) | ConvertFrom-Json
    $Agent = "$($Info.agent)"
    if (-not $Agent) { Stop-Install 'не удалось узнать имя агента (sysai-agent status)' }

    # 4. Hermes Agent
    if (-not (Test-Command 'hermes')) {
        Say 'ставлю Hermes Agent (Nous Research), это несколько минут...'
        $tmp = Join-Path $env:TEMP 'sysai-hermes-install.ps1'
        $text = Invoke-RestMethod 'https://hermes-agent.nousresearch.com/install.ps1'
        if ($text -is [byte[]]) { $text = [Text.Encoding]::UTF8.GetString($text) }
        $text = "$text".TrimStart([char]0xFEFF)
        [IO.File]::WriteAllText($tmp, $text, (New-Object Text.UTF8Encoding $true))
        & powershell -NoProfile -ExecutionPolicy Bypass -File $tmp -NonInteractive
        Remove-Item $tmp -ErrorAction SilentlyContinue
        Update-PathNow
        if (-not (Test-Command 'hermes')) { Stop-Install 'Hermes не установился. Поставьте его по https://hermes-agent.nousresearch.com и запустите установщик снова' }
    }

    # 5. профиль Hermes агента — без встроенных навыков (нужные придут из SYSAI_Hub)
    & hermes profile show $Agent *> $null
    if ($LASTEXITCODE -ne 0) {
        Say "создаю профиль Hermes $Agent..."
        & hermes profile create $Agent --no-skills
        if ($LASTEXITCODE -ne 0) { Stop-Install "профиль Hermes $Agent не создался, см. сообщения выше" }
    }
    $ProfileDir = ''
    $show = (& hermes profile show $Agent 2> $null | Out-String)
    if ($show -match 'Path:\s*(\S[^\r\n]*)') { $ProfileDir = $Matches[1].Trim() }

    # 6. первая синхронизация: доступы, характер, навыки
    Say 'применяю доступы, характер и навыки из SYSAI...'
    if ($ProfileDir) { & sysai-agent sync --hermes-home $ProfileDir } else { & sysai-agent sync }
    if ($LASTEXITCODE -ne 0) { Warn 'синхронизация прошла с ошибками (см. выше): синхронизатор повторит сам' }

    # 7. окно агента «Агент SYSAI»: Electron и приложение, проверенные по подписи SYSAI, ярлыки на рабочем столе и в «Пуске»
    Say 'ставлю окно агента «Агент SYSAI» (в первый раз скачивается около 160 МБ)...'
    & sysai-agent desktop --install-only
    $DesktopOK = ($LASTEXITCODE -eq 0)
    if (-not $DesktopOK) { Warn 'окно агента не установилось (см. выше): синхронизатор попробует сам, или выполните sysai-agent desktop' }

    # 8. синхронизация в фоне: при входе в Windows и каждые 5 минут
    $runner = Get-Command 'sysai-agentw' -ErrorAction SilentlyContinue
    if (-not $runner) { $runner = Get-Command 'sysai-agent' }
    $exe = $runner.Source
    $runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    New-ItemProperty -Path $runKey -Name 'SYSAI Agent' -Value ('"' + $exe + '" run') -PropertyType String -Force | Out-Null
    Start-Process -FilePath $exe -ArgumentList 'run' -WindowStyle Hidden
    Say 'синхронизация включена: при входе в Windows и каждые 5 минут'

    Write-Host ''
    Say "готово: агент $Agent на этом компьютере."
    if ($DesktopOK) { Write-Host '  Окно агента:           ярлык «Агент SYSAI» на рабочем столе (или sysai-agent desktop)' }
    else { Warn 'окно и ярлык ещё не установлены; проверьте сообщения выше и запустите sysai-agent desktop' }
    Write-Host "  Агент в терминале:     hermes -p $Agent"
    Write-Host '  Что на компьютере:     sysai-agent status'
    Write-Host "  Доступы, расходы, трасса - на странице агента в SYSAI: $Server/agents"
}
catch {
    if ("$_" -ne 'SYSAI-STOP') { Write-Host "SYSAI: установка прервалась: $_" -ForegroundColor Red }
}
}
