const fs = require('fs')

const projectRoot = __dirname
const localPython = `${projectRoot}/.venv/bin/python`
const pythonBin = process.env.PYTHON_BIN || (fs.existsSync(localPython) ? localPython : 'python3')
const autodlTokenFile = `${projectRoot}/.secrets/autodl_api_token`

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
        ALPHA_AUTODL_API_TOKEN_FILE: process.env.ALPHA_AUTODL_API_TOKEN_FILE || autodlTokenFile,
        ...(process.env.ALPHA_AUTODL_API_TOKEN
          ? { ALPHA_AUTODL_API_TOKEN: process.env.ALPHA_AUTODL_API_TOKEN }
          : {}),
      },
    },
  ],
}
