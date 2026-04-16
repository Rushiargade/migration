@echo off
set REMOTE_HOST=root@10.5.5.113
set REMOTE_DIR=/opt/migration/

echo === Building Docker image ===
docker-compose build
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Docker build failed!
    pause
    exit /b %ERRORLEVEL%
)

echo === Saving Docker image to vmigrate-update.tar ===
docker save migration-vmigrate-web:latest -o vmigrate-update.tar
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Docker save failed!
    pause
    exit /b %ERRORLEVEL%
)

echo === Transferring and deploying via Automated Docker SSHPass ===
docker run --rm -v "%cd%":/work -w /work alpine sh -c "apk add --quiet --no-cache openssh-client sshpass && sshpass -p 'supp0rt$ESDS' scp -o StrictHostKeyChecking=no -r vmigrate-update.tar docker-compose.yml config %REMOTE_HOST%:%REMOTE_DIR% && sshpass -p 'supp0rt$ESDS' ssh -o StrictHostKeyChecking=no %REMOTE_HOST% 'cd %REMOTE_DIR% && docker load -i vmigrate-update.tar && docker-compose down && docker-compose up -d'"
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Automated transfer and deploy failed!
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo =======================================================
echo Update successfully applied and backend restarted!
echo =======================================================
pause
