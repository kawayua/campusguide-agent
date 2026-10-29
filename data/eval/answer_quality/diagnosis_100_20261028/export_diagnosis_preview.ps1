$ErrorActionPreference = 'Stop'
$analysisRoot = Join-Path (Get-Location) 'data\eval\answer_quality\diagnosis_100_20261028'
$diagnosisDocx = Join-Path (Get-Location) 'docs\reports\CampusGuide_100题逐题错误分析与修改建议_20261028.docx'
$previewPdf = Join-Path $analysisRoot 'diagnosis_preview.pdf'
$wordPreviewApp = $null
$previewDoc = $null
try {
    $wordPreviewApp = New-Object -ComObject Word.Application
    $wordPreviewApp.Visible = $false
    $wordPreviewApp.DisplayAlerts = 0
    $previewDoc = $wordPreviewApp.Documents.Open($diagnosisDocx, $false, $true)
    $previewDoc.Repaginate()
    $previewPages = $previewDoc.ComputeStatistics(2)
    $previewDoc.ExportAsFixedFormat($previewPdf, 17)
    [PSCustomObject]@{ Pdf = $previewPdf; Pages = $previewPages; Bytes = (Get-Item -LiteralPath $previewPdf).Length } | ConvertTo-Json
} finally {
    if ($null -ne $previewDoc) { $previewDoc.Close(0); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($previewDoc) }
    if ($null -ne $wordPreviewApp) { $wordPreviewApp.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($wordPreviewApp) }
}
