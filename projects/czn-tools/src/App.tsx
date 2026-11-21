import { NavLink, Navigate, Route, Routes } from 'react-router'
import { TOOLS } from './tools'

const REPO_URL = 'https://github.com/rcdailey/chaos-zero-nightmare'
// GitHub mark from simple-icons (icons/github.svg), 24x24 viewBox.
const GITHUB_MARK =
  'M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 ' +
  '0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 ' +
  '17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 ' +
  '1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465' +
  '-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 ' +
  '3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 ' +
  '3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 ' +
  '1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0' +
  '-6.627-5.373-12-12-12'

export default function App() {
  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <nav
        className="flex shrink-0 items-center gap-1 border-nord-2 bg-nord-1 p-4 md:sticky
        md:top-0 md:h-screen md:w-56 md:flex-col md:items-stretch md:border-r"
      >
        <h1
          className="mb-3 hidden px-3 text-sm font-semibold tracking-wide text-nord-8
          uppercase md:block"
        >
          CZN Tools
        </h1>
        {TOOLS.map((tool) => (
          <NavLink
            key={tool.path}
            to={`/${tool.path}`}
            className={({ isActive }) =>
              `rounded-lg px-3 py-2 text-sm transition ${
                isActive ? 'bg-nord-2 font-semibold text-nord-6' : 'text-nord-4 hover:bg-nord-2/50'
              }`
            }
          >
            {tool.name}
          </NavLink>
        ))}
        <a
          href={REPO_URL}
          target="_blank"
          rel="noreferrer"
          className="ml-auto flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-nord-4
            transition hover:bg-nord-2/50 hover:text-nord-6 md:mt-auto md:ml-0"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true" className="size-5 fill-current">
            <path d={GITHUB_MARK} />
          </svg>
          GitHub
        </a>
      </nav>
      <main className="min-w-0 flex-1 p-6">
        <Routes>
          {TOOLS.map(({ path, component: Tool }) => (
            <Route key={path} path={path} element={<Tool />} />
          ))}
          <Route path="*" element={<Navigate to={`/${TOOLS[0].path}`} replace />} />
        </Routes>
      </main>
    </div>
  )
}
