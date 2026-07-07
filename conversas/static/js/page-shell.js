/**
 * CONV-MOBILE-RESPONSIVE-02 — drawer mobile das paginas Templates/Settings.
 *
 * Em <=640px a .page-sidebar vira drawer off-canvas (CSS); este script so
 * liga o estado: hamburguer abre, backdrop/navegacao fecham. Estado por
 * classe no <body> (body.conv-mobile-menu-open), ativado apenas sob
 * matchMedia — desktop intocado. Sem dependencias, sem keydown global;
 * qualquer falha aqui nao afeta a pagina.
 */
(function () {
    'use strict';

    var mq = window.matchMedia('(max-width: 640px)');
    var sidebar = document.querySelector('.page-sidebar');
    var menuBtn = document.getElementById('pageMenuBtn');
    var backdrop = document.getElementById('pageDrawerBackdrop');
    if (!sidebar || !menuBtn || !backdrop) return;

    function openMenu() {
        if (mq.matches) document.body.classList.add('conv-mobile-menu-open');
    }

    function closeMenu() {
        document.body.classList.remove('conv-mobile-menu-open');
    }

    menuBtn.addEventListener('click', openMenu);
    backdrop.addEventListener('click', closeMenu);
    // tocar num item de navegacao fecha o drawer (a pagina vai trocar)
    sidebar.addEventListener('click', function (e) {
        if (e.target.closest && e.target.closest('a')) closeMenu();
    });
    // voltar ao desktop nunca deixa o backdrop/estado preso
    if (mq.addEventListener) {
        mq.addEventListener('change', function (e) {
            if (!e.matches) closeMenu();
        });
    }
})();
