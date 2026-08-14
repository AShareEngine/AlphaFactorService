const fs = require('fs')

const projectRoot = __dirname
const localPython = `${projectRoot}/.venv/bin/python`
const pythonBin = process.env.PYTHON_BIN || (fs.existsSync(localPython) ? localPython : 'python3')
const researchPython = `${projectRoot}/.venv-research/bin/python`
const researchPythonBin = process.env.AB_FACTOR_RESEARCH_PYTHON_BIN
  || (fs.existsSync(researchPython) ? researchPython : pythonBin)

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
      },
    },
    {
      name: 'alpha-factor-research-worker',
      cwd: projectRoot,
      script: researchPythonBin,
      args: ['-m', 'factor_service.research.cli', 'run'],
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
