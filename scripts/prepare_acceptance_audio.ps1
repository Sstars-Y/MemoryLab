$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$projectRoot = Split-Path -Parent $PSScriptRoot
$fixtureRoot = Join-Path $projectRoot '.local\verification\acceptance-20261114\fixtures'
New-Item -ItemType Directory -Path $fixtureRoot -Force | Out-Null
$samples = @(
    @{ name='umbrella-old'; text='这是虚构的验收内容。红伞放在北门。验收编号是六八四二。' },
    @{ name='umbrella-new'; text='补充刚才的验收内容。后来把红伞移到了南门。' },
    @{ name='negation'; text='注意，我不喜欢甜咖啡。我喜欢不加糖的茶。' },
    @{ name='identifier'; text='蓝色测试箱的编号是七三九一，不是七三九二。' },
    @{ name='unrelated'; text='这是其他话题。图片里有三只蓝色气球，背景是一座黄色房子。' }
)
$speech = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $speech.SelectVoice('Microsoft Huihui Desktop')
    $speech.Rate = -1
    $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
    foreach ($sample in $samples) {
        $speech.SetOutputToWaveFile((Join-Path $fixtureRoot ($sample.name + '.wav')), $format)
        $speech.Speak($sample.text)
        $speech.SetOutputToNull()
    }
} finally {
    $speech.Dispose()
}
$samples | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $fixtureRoot 'audio-fixtures.json') -Encoding utf8
Write-Output 'Generated 5 synthetic Chinese speech fixtures.'
