# Registra (ou atualiza) as tarefas do goofish-miner no Agendador de Tarefas do Windows:
#   GoofishMiner          - coletor a cada 15 min (ele decide se esta na janela 08-20h
#                         de Brasilia e se ja passou o intervalo minimo)
#   GoofishMinerTelegram  - escutador do bot (/status, /ultimos, /forcar,
#                         /parar), ao fazer logon e a cada 5 min se tiver caido
#
# Os dois rodam com pythonw.exe do venv: processo SEM console. No Windows 11 o
# terminal padrao e o Windows Terminal, e um processo de console lancado pela
# tarefa vira uma aba dele — fechar o terminal matava o escutador
# (LastTaskResult 0xC000013A, STATUS_CONTROL_C_EXIT, em 2026-09-08).
# Consequencia: as tarefas NAO passam por `uv run`; depois de mudar dependencias,
# rode `uv sync` uma vez.
#
#   powershell -ExecutionPolicy Bypass -File scripts\agendar_tarefa.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\agendar_tarefa.ps1 -Remover
#
# -AllowStartIfOnBatteries e -DontStopIfGoingOnBatteries sao OBRIGATORIOS: por
# padrao o Agendador PARA a tarefa quando o note sai da tomada e nao deixa
# reiniciar enquanto estiver na bateria. Em 2026-09-10, as 22h, isso derrubou o
# bot E a coleta por 14 horas (168 disparos perdidos no escutador, 56 no coletor).
#
# (sem acentos de proposito: PowerShell 5.1 le .ps1 sem BOM como ANSI)

param([switch]$Remover)

$raiz = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$pythonw = Join-Path $raiz ".venv\Scripts\pythonw.exe"

function Matar-Escutadores {
    # qualquer processo rodando o escutador, venha da tarefa ou de um terminal aberto na mao.
    # NAO matar pelo caminho do venv: isso derruba a varredura em andamento.
    Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -like "*goofish_miner.telegram_bot*" -or $_.Name -eq "goofish-miner-telegram.exe"
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Remover) {
    foreach ($n in @("GoofishMiner", "GoofishMinerTelegram")) {
        Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "tarefa '$n' removida"
    }
    Matar-Escutadores
    exit 0
}

if (-not (Test-Path $pythonw)) { throw "pythonw nao encontrado em $pythonw (rode 'uv sync' na raiz do projeto)" }

function Acao($modulo) {
    New-ScheduledTaskAction -Execute $pythonw -Argument "-m $modulo" -WorkingDirectory $raiz
}

# --- coletor: a tarefa dispara a cada 15 min; a frequencia REAL e a do coletor
#     (coleta.toml `intervalo_min_minutos`, ou o que o bot gravou com /frequencia).
#     Fora da vez ele sai em menos de um segundo.
$gatilho = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650)
$config = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 50) `
    -StartWhenAvailable -MultipleInstances IgnoreNew -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "GoofishMiner" -Action (Acao "goofish_miner.coletor") -Trigger $gatilho -Settings $config -Force `
    -Description "Varredura do Goofish (goofish-miner). Janela e frequencia: coleta.toml ou /janela e /frequencia no bot. Sem console (pythonw)." | Out-Null
Write-Host "tarefa 'GoofishMiner' registrada: pythonw -m goofish_miner.coletor em $raiz, gatilho a cada 15 min"

# --- escutador do Telegram, continuo
# Dois gatilhos: no logon, e a cada 5 min. O segundo cura sozinho: se o processo
# morreu, a proxima repeticao levanta de novo; se esta vivo, IgnoreNew nao faz nada.
$gatilhoLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$gatilhoLoop = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$configTg = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew `
    -StartWhenAvailable -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "GoofishMinerTelegram" -Action (Acao "goofish_miner.telegram_bot") `
    -Trigger $gatilhoLogon, $gatilhoLoop -Settings $configTg -Force `
    -Description "Escutador do bot do Telegram. Sem console (pythonw). Reinicia sozinho a cada 5 min se cair." | Out-Null
Stop-ScheduledTask -TaskName "GoofishMinerTelegram" -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Matar-Escutadores
Start-ScheduledTask -TaskName "GoofishMinerTelegram"
Write-Host "tarefa 'GoofishMinerTelegram' registrada e (re)iniciada"

Get-ScheduledTask -TaskName "GoofishMiner", "GoofishMinerTelegram" | Select-Object TaskName, State
