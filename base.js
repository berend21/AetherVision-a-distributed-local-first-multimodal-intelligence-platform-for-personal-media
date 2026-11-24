

let videosLoaded = false;
let videosLoading = false;

async function loadVideosSection() {
    let container = document.getElementById('videosView');

    if (!container) {
        container = document.createElement('div');
        container.id = 'videosView';
        container.className = 'videos-view';

        const main = document.querySelector('main');

        main.insertBefore(
            container,
            document.getElementById('placesView')
        );
    }
    const main = document.querySelector('main');

    if (container.parentElement !== main) {
        main.insertBefore(
            container,
            document.getElementById('placesView')
        );
    }

    container.hidden = false;

    if (videosLoading) return;

    if (videosLoaded) {
        await window.VideosView?.refresh();
        return;
    }

    videosLoading = true;

    try {
        const response = await fetch('/videos', {
            credentials: 'same-origin'
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const html = await response.text();

        const doc = new DOMParser().parseFromString(
            html,
            'text/html'
        );

        const content = doc.querySelector('#videosView');

        if (!content) {
            throw new Error('Videos HTML not found');
        }

        container.replaceChildren(
            ...Array.from(content.childNodes).map(node =>
                document.importNode(node, true)
            )
        );

        if (!document.getElementById('videosStylesheet')) {
            const stylesheet = doc.querySelector(
                'link[href*="videos.css"]'
            );

            if (stylesheet) {
                const link = document.createElement('link');
                link.id = 'videosStylesheet';
                link.rel = 'stylesheet';
                link.href = stylesheet.href;

                document.head.appendChild(link);
            }
        }

        if (!window.VideosView) {
            await new Promise((resolve, reject) => {
                const script = document.createElement('script');

                script.src = '/static/js/videos.js';
                script.onload = resolve;
                script.onerror = reject;

                document.body.appendChild(script);
            });
        }

        if (!window.VideosView) {
            throw new Error('VideosView was not registered');
        }

        videosLoaded = true;

        await window.VideosView.init();

        console.log('[Videos] Initialized successfully');

    } catch (error) {
        console.error('[Videos] Initialization failed:', error);
        showToast('Could not load videos', 'error');

    } finally {
        videosLoading = false;
    }
}

function updateSidebarPosition() {
    const header = document.querySelector('.photos-header');

    if (!header) return;

    document.documentElement.style.setProperty(
        '--app-header-height',
        `${header.getBoundingClientRect().height}px`
    );
}

updateSidebarPosition();

window.addEventListener('resize', updateSidebarPosition);

if ('ResizeObserver' in window) {
    new ResizeObserver(updateSidebarPosition).observe(
        document.querySelector('.photos-header')
    );
}

async function fetchJson(url, options = {}) {
    const method = (options.method || 'GET').toUpperCase();
    const headers = new Headers(options.headers || {});

    if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
        const token = document.querySelector(
            'meta[name="csrf-token"]'
        )?.content;

        if (!token) {
            throw new Error('CSRF token is missing. Reload the page.');
        }

        headers.set('X-CSRFToken', token);
    }

    const response = await fetch(url, {
        ...options,
        method,
        headers,
        credentials: 'same-origin'
    });

    if (!response.ok) {
        let message = `Request failed (${response.status})`;

        try {
            const body = await response.json();
            if (typeof body.error === 'string' && body.error) {
                message = body.error;
            }
        } catch {}

        throw new Error(message);
    }

    return response.json();
}

function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = 'toast ' + type;
    toast.textContent = message;

    document.getElementById('toastContainer').appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.3s';
        setTimeout(() => toast.remove(), 300);
    }, 2500);
}

function debounce(fn, delay) {
    let timer;

    return (...args) => {
        clearTimeout(timer);
        timer = setTimeout(() => fn(...args), delay);
    };
}

document.querySelectorAll('button.sidebar-item').forEach(button => {
    button.addEventListener('click', async () => {
        const section = button.dataset.section;
        console.log('[DEBUG] Sidebar clicked:', section);

        console.log(
            '[DEBUG] Selection button before navigation:',
            document.getElementById('selectMode')?.className
        );
        if (window.location.pathname === '/videos') {

            if (section === 'videos') {
                return;
            }
            window.location.href =
                '/?section=' + encodeURIComponent(section);

            return;
        }

        const isPlaces = section === 'places';
        const isVideos = section === 'videos';
        const isPhotos = section === 'photos';

        if (!isVideos) {

            if (window.VideosView?.resetHeader) {
                window.VideosView.resetHeader();
            }

            if (isPhotos && window.PhotosView?.updateHeader) {
                await window.PhotosView.updateHeader();
            }
        }

        if (section === 'photos') {
            window.PhotosView?.exitSelectionMode();
        }
        document.querySelectorAll('.sidebar-item').forEach(item => {
            item.classList.remove('active');
            item.removeAttribute('aria-current');
        });

        button.classList.add('active');
        button.setAttribute('aria-current', 'page');

        const setHidden = (id, hidden) => {
            const element = document.getElementById(id);
            if (element) element.hidden = hidden;
        };

        setHidden('placesView', !isPlaces);
        setHidden('videosView', !isVideos);

        setHidden('gallery', !isPhotos);
        setHidden('empty', !isPhotos);
        setHidden('loading', !isPhotos);
        setHidden('sentinel', !isPhotos);

        if (!isPhotos) {
            setHidden('selectionToolbar', true);
        }

        setHidden('videoGallery', !isVideos);
        setHidden('videoEmpty', !isVideos);
        setHidden('videoLoading', !isVideos);
        setHidden('videoSentinel', !isVideos);
        setHidden('videoSelectionToolbar', !isVideos);

        if (isPlaces) {
            if (window.PlacesView?.init) {
                await window.PlacesView.init();
            } else {
                console.error('PlacesView is not loaded');
            }
        }

        if (isVideos) {
            console.log('[Videos] Sidebar clicked');

            await loadVideosSection();

            console.log('[Videos] Container:', document.getElementById('videosView'));
            console.log('[Videos] Gallery:', document.getElementById('videoGallery'));
            console.log('[Videos] VideosView:', window.VideosView);
        }
    });
});
document.addEventListener('DOMContentLoaded', () => {
    const requestedSection =
        new URLSearchParams(window.location.search).get('section');

    if (!requestedSection) return;

    const button = [
        ...document.querySelectorAll('.sidebar-item')
    ].find(item => item.dataset.section === requestedSection);


    if (button) {
        button.click();
    }

    window.history.replaceState(
        {},
        document.title,
        window.location.pathname
    );
});
