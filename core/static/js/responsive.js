(function () {
  function initResponsiveNavigation() {
    const button = document.getElementById('mobileMenuBtn');
    const sidebar = document.getElementById('appSidebar');
    const overlay = document.getElementById('mobileSidebarOverlay');

    if (!button || !sidebar || !overlay) return;

    const closeMenu = () => {
      document.body.classList.remove('sidebar-open');
      sidebar.classList.remove('mobile-open');
      button.setAttribute('aria-expanded', 'false');
      button.setAttribute('aria-label', 'Abrir menú');
    };

    const openMenu = () => {
      document.body.classList.add('sidebar-open');
      sidebar.classList.add('mobile-open');
      button.setAttribute('aria-expanded', 'true');
      button.setAttribute('aria-label', 'Cerrar menú');
    };

    button.addEventListener('click', function () {
      if (sidebar.classList.contains('mobile-open')) closeMenu();
      else openMenu();
    });

    overlay.addEventListener('click', closeMenu);

    sidebar.addEventListener('click', function (event) {
      if (window.innerWidth <= 900 && event.target.closest('a')) closeMenu();
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') closeMenu();
    });

    window.addEventListener('resize', function () {
      if (window.innerWidth > 900) closeMenu();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initResponsiveNavigation);
  } else {
    initResponsiveNavigation();
  }
})();
