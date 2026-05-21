const fs = require('fs')

const projectRoot = __dirname
const localPython = `${projectRoot}/.venv/bin/python`
const pythonBin = process.env.PYTHON_BIN || (fs.existsSync(localPython) ? localPython : 'python3')
const factorHost = process.env.AB_FACTOR_HOST || '0.0.0.0'
const factorPort = process.env.AB_FACTOR_PORT || '8100'

module.exports = {
  apps: [
    {
      name: 'alpha-factor-service',
      cwd: projectRoot,
      script: pythonBin,
      args: ['-m', 'factor_service.main'],
      interpreter: 'none',
      autorestart: true,
      watch: false,
      max_restarts: 10,
      min_uptime: '5s',
      env: {
        PYTHONUNBUFFERED: '1',
        AB_FACTOR_HOST: factorHost,
        AB_FACTOR_PORT: factorPort,
      },
    },
    {
      name: 'alpha-factor-worker',
      cwd: projectRoot,
      script: pythonBin,
      args: ['-m', 'factor_service.worker', '--limit', '5', '--poll-interval', '60'],
      interpreter: 'none',
      autorestart: true,
      watch: false,
      max_restarts: 10,
      min_uptime: '5s',
      env: {
        PYTHONUNBUFFERED: '1',
      },
    },
  ],
}
