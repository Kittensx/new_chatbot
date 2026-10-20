Write-Host "=======================================================" -ForegroundColor Red
Write-Host "   FORCE-STOPPING ALL ACTIVE RUNNING CONTAINERS        " -ForegroundColor Red
Write-Host "=======================================================" -ForegroundColor Red

# 1. Query and forcefully stop all running container instances
\$runningContainers = docker ps -q
if (\$runningContainers) {
    Write-Host "Found active runtime environments. Halting containers safely..."
    docker stop \$runningContainers
} else {
    Write-Host "No active container conflicts found in memory." -ForegroundColor Green
}

Write-Host "`n=======================================================" -ForegroundColor Yellow
Write-Host "   PURGING INACTIVE CONTAINER SHELLS & CACHES          " -ForegroundColor Yellow
Write-Host "=======================================================" -ForegroundColor Yellow

# 2. Delete the dead/stopped container shells holding onto system files
\$allContainers = docker ps -a -q
if (\$allContainers) {
    Write-Host "Removing inactive container structural instances..."
    docker rm \$allContainers
}

Write-Host "`n=======================================================" -ForegroundColor Green
Write-Host "   RECLAIMING HARD DRIVE AND VRAM MEMORY MATRIX        " -ForegroundColor Green
Write-Host "=======================================================" -ForegroundColor Green

# 3. Force-trigger a system prune to wipe build caches and untagged tracking blocks
# The -f flag bypasses the manual [Y/N] confirmation prompt for fast automation.
docker system prune -f

Write-Host "`n🎉 System fully optimized! All storage space and GPU VRAM reclaimed cleanly." -ForegroundColor Green
