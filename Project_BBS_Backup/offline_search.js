const dialog = document.getElementById('search-dialog');
const searchForm = document.getElementById('search-form');
const queryInput = document.getElementById('search-query');
const searchLabel = document.getElementById('search-label');
const searchStatus = document.getElementById('search-status');
const searchResults = document.getElementById('search-results');
const searchMore = document.getElementById('search-more');
const searchScroll = document.getElementById('search-scroll');
const searchModes = {'?': 'all', '/': 'title', a: 'author', '#': 'aid', S: 'subject', '!': 'exclude'};
const labels = {all: '全板搜尋', title: '搜尋標題', author: '搜尋作者帳號',
                aid: '搜尋 AID', subject: '搜尋同主題', exclude: '排除關鍵字'};
let dataLoad;
let searchMode;
let baseFilter;
let results = [];
let renderedCount = 0;
let searchRequest = 0;
let previousFocus;
let selectedResult;

function selectResult(link) {
    if (selectedResult && selectedResult !== link) {
        selectedResult.removeAttribute('aria-current');
        selectedResult.tabIndex = -1;
    }
    selectedResult = link;
    link.setAttribute('aria-current', 'true');
    link.tabIndex = 0;
    link.focus({preventScroll: true});
    link.scrollIntoView({block: 'nearest'});
}

function resetResults() {
    selectedResult = null;
    searchResults.replaceChildren();
    searchMore.hidden = true;
    renderedCount = 0;
    searchScroll.scrollTop = 0;
}

function boardSearchRows(board) {
    if (!dataLoad) {
        dataLoad = new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = 'search-data.js';
            script.onload = () => window.bbsSearchData ? resolve(window.bbsSearchData) : reject();
            script.onerror = () => reject();
            document.head.append(script);
        });
    }
    return dataLoad.then(data => data[board.name] ?? []);
}

function normalize(value) {
    return value.trim().toLowerCase();
}

function subject(value) {
    return normalize(value.replace(/^(?:\s*re\s*:\s*)+/i, ''));
}

function filterRows(rows, mode, value) {
    const term = normalize(value);
    switch (mode) {
        case 'title': return rows.filter(row => normalize(row.title).includes(term));
        case 'author': return rows.filter(row => normalize(row.author).split(/[\s(（]/, 1)[0] === term);
        case 'aid': return rows.filter(row => row.aid === value.trim().replace(/^#/, ''));
        case 'subject': return rows.filter(row => row.subject === subject(value));
        default: return rows.filter(row => normalize(row.title).includes(term) ||
                                           row.body.toLowerCase().includes(term));
    }
}

function appendResults() {
    const batch = results.slice(renderedCount, renderedCount + 50);
    for (const {row, term} of batch) {
        const item = document.createElement('li');
        const link = document.createElement('a');
        link.href = 'articles/' + row.key + '.html';
        link.textContent = [row.index, row.title, row.author].filter(Boolean).join(' · ');
        link.tabIndex = -1;
        link.addEventListener('focus', () => {
            if (selectedResult !== link) selectResult(link);
        });
        item.append(link);
        if (term) {
            const start = row.body.toLowerCase().indexOf(term);
            if (start >= 0) {
                const excerpt = document.createElement('span');
                const before = Math.max(0, start - 24);
                const end = Math.min(row.body.length, start + term.length + 48);
                excerpt.className = 'meta';
                excerpt.textContent = (before ? '…' : '') + row.body.slice(before, end).replace(/\s+/g, ' ')
                    + (end < row.body.length ? '…' : '');
                item.append(excerpt);
            }
        }
        searchResults.append(item);
    }
    renderedCount += batch.length;
    searchMore.hidden = renderedCount >= results.length;
}

async function runSearch(value) {
    const request = ++searchRequest;
    const board = activeBoard;
    const mode = searchMode;
    const prior = baseFilter?.board === board ? baseFilter : null;
    resetResults();
    searchStatus.textContent = '讀取本地搜尋資料…';
    try {
        const rows = await boardSearchRows(board);
        if (request !== searchRequest || !dialog.open) return;
        let matches = filterRows(rows, mode, value);
        if (mode === 'exclude') {
            const excluded = new Set(matches);
            matches = (prior?.rows ?? rows).filter(row => !excluded.has(row));
        }
        const term = mode === 'all' ? normalize(value) : mode === 'exclude' ? (prior?.term ?? '') : '';
        baseFilter = {board, rows: matches, term};
        results = matches.slice().reverse().map(row => ({row, term}));
        searchStatus.textContent = `找到 ${results.length} 篇 · ${board.name}`;
        appendResults();
        const first = searchResults.querySelector('a');
        if (first) selectResult(first);
    } catch {
        if (request === searchRequest && dialog.open)
            searchStatus.textContent = '無法讀取搜尋資料。請確認備份資料夾完整，或執行離線內容重整。';
    }
}

async function openSearch(mode) {
    if (!activeBoard) return;
    const board = activeBoard;
    const entry = board.entries[board.position];
    const request = ++searchRequest;
    searchMode = mode;
    searchLabel.textContent = labels[mode];
    queryInput.value = '';
    resetResults();
    searchStatus.textContent = board.name;
    if (!dialog.open) {
        previousFocus = document.activeElement;
        dialog.showModal();
    }
    queryInput.focus();
    if (mode !== 'subject') return;
    try {
        const rows = await boardSearchRows(board);
        if (request !== searchRequest || !dialog.open) return;
        const current = rows.find(row => 'a-' + row.key === entry.id);
        if (current) {
            queryInput.value = current.title;
            runSearch(current.title);
        } else {
            searchStatus.textContent = '目前選取的項目沒有已存文章。';
        }
    } catch {
        if (request === searchRequest && dialog.open)
            searchStatus.textContent = '無法讀取搜尋資料。請執行離線內容重整。';
    }
}

searchForm.addEventListener('submit', event => {
    event.preventDefault();
    const value = queryInput.value.trim();
    if (value) runSearch(value);
});
queryInput.addEventListener('input', () => { searchRequest++; });
document.querySelectorAll('.open-search').forEach(button => {
    button.onclick = () => {
        const board = boards.find(candidate => candidate.pages.includes(button.closest('.board').querySelector('.entries')));
        if (board && board !== activeBoard && board.entries.length) select(board, Math.max(0, board.position));
        openSearch('all');
    };
});
document.getElementById('search-close').onclick = () => dialog.close();
searchMore.onclick = () => {
    const first = renderedCount;
    appendResults();
    const link = searchResults.querySelectorAll('a')[first];
    if (link) selectResult(link);
};
dialog.addEventListener('close', () => {
    searchRequest++;
    previousFocus?.focus();
});
document.addEventListener('keydown', event => {
    if (!dialog.open || event.isComposing || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === 'Escape') {
        event.preventDefault();
        dialog.close();
        return;
    }
    if (event.key === 'Enter' && selectedResult && !event.target.closest?.('input, button, a')) {
        event.preventDefault();
        selectedResult.click();
        return;
    }
    if (!selectedResult || !['ArrowDown', 'ArrowUp'].includes(event.key)) return;
    const links = Array.from(searchResults.querySelectorAll('a'));
    const position = links.indexOf(selectedResult);
    event.preventDefault();
    if (position === links.length - 1 && event.key === 'ArrowDown' && !searchMore.hidden) {
        searchMore.click();
    } else {
        const next = Math.max(0, Math.min(links.length - 1,
            position + (event.key === 'ArrowUp' ? -1 : 1)));
        selectResult(links[next]);
    }
});

document.addEventListener('keydown', event => {
    if (event.isComposing || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey ||
        dialog.open || event.target.matches('input, textarea, [contenteditable="true"]')) return;
    const key = event.key;
    if (key === 'h' || key === 'H') {
        event.preventDefault();
        location.href = 'ReadMe.html#keyboard';
    } else if (searchModes[key]) {
        event.preventDefault();
        openSearch(searchModes[key]);
    }
});

const initialMode = location.hash.slice(1).split('/')[1];
if (Object.hasOwn(labels, initialMode)) openSearch(initialMode);
