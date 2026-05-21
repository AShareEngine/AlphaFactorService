const fs = require('fs')

const projectRoot = __dirname
const localPython = `${projectRoot}/.venv/bin/python`
const pythonBin = process.env.PYTHON_BIN || (fs.existsSync(localPython) ? localPython : 'python3')
const factorHost = process.env.AB_FACTOR_HOST || '0.0.0.0'
const factorPort = process.env.AB_FACTOR_PORT || '8100'
const runtimeConfig =
  process.env.AB_FACTOR_RUNTIME_CONFIG ||
  process.env.SYNC_DATA_RUNTIME_CONFIG ||
  process.env.ALPHABLOCKS_SYNC_DATA_RUNTIME_CONFIG ||
  process.env.ALPHABLOCKS_RUNTIME_CONFIG ||
  process.env.RUNTIME_CONFIG_PATH ||
  '/Users/zhao/Desktop/git/AlphaBlocksSyncData/config/runtime.local.yaml'
const factorDatabase = process.env.AB_FACTOR_CLICKHOUSE_DATABASE || 'ab_factor'

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
        AB_FACTOR_RUNTIME_CONFIG: runtimeConfig,
        AB_FACTOR_CLICKHOUSE_DATABASE: factorDatabase,
      },
    },
  ],
}
