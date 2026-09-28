/**
 * 应用外壳。负责三件事：未登录时挡在登录页、把当前工作空间的名字交给顶栏、窄屏时管住抽屉。
 */

import { useEffect, useState } from 'react';
import { Outlet, useLocation } from 'react-router-dom';

import { Login } from './Login';
import { Sidebar } from './Sidebar';
import { TopBar } from './TopBar';
import { useAuth } from './auth';
import { ReadinessProvider } from './readiness';
import { WORKSPACES } from '@/domain/labels';

const FALLBACK = { label: '工作台', description: '' };

export function AppShell() {
  const { token } = useAuth();
  const location = useLocation();
  const [navOpen, setNavOpen] = useState(false);

  // 换工作空间就收起抽屉，否则窄屏上点完导航还要手动关一次。
  useEffect(() => {
    setNavOpen(false);
  }, [location.pathname]);

  // 抽屉打开时锁住背景滚动；否则在触屏上会「滑到后面那一层」。
  useEffect(() => {
    if (!navOpen) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previous;
    };
  }, [navOpen]);

  // Esc 关抽屉：键盘用户不该被迫去够那个遮罩。
  useEffect(() => {
    if (!navOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setNavOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [navOpen]);

  if (!token) return <Login />;

  const workspace =
    WORKSPACES.find((item) => location.pathname === item.path || location.pathname.startsWith(`${item.path}/`)) ?? FALLBACK;

  return (
    <ReadinessProvider>
      <a className="qp-skip" href="#qp-main">
        跳到主内容
      </a>
      <div className="qp-app">
        <Sidebar open={navOpen} onNavigate={() => setNavOpen(false)} />
        <button
          type="button"
          className={`qp-scrim${navOpen ? ' is-open' : ''}`}
          aria-label="关闭导航"
          tabIndex={navOpen ? 0 : -1}
          onClick={() => setNavOpen(false)}
        />
        <div className="qp-main">
          <TopBar
            title={workspace.label}
            description={workspace.description}
            onToggleNav={() => setNavOpen((value) => !value)}
          />
          <main className="qp-content" id="qp-main" tabIndex={-1}>
            <Outlet />
          </main>
        </div>
      </div>
    </ReadinessProvider>
  );
}
