if (window.htmx) {
  htmx.config.responseHandling = [
    { code: '204', swap: false },
    { code: '[23]..', swap: true },
    { code: '[45]..', swap: true, error: true },
  ];
}

function dirPicker() {
  return {
    open: false,
    dataRoots: [],
    current_root: '',
    current: '',
    relative: '',
    parent: '',
    crumb: [],
    dirs: [],
    loading: true,
    error: '',
    filter: '',
    selected: null,
    creating: false,
    newName: '',
    createError: '',

    init() {
      this.load('');
    },

    async load(path) {
      this.loading = true;
      this.error = '';
      this.selected = null;
      const url = path
        ? '/browse/tree?path=' + encodeURIComponent(path)
        : '/browse/tree';
      try {
        const res = await fetch(url);
        const data = await res.json();
        if (!res.ok) {
          this.error = data.error || 'Failed to load folders';
          return;
        }
        this.dataRoots = data.data_roots || [];
        this.current_root = data.current_root;
        this.current = data.current;
        this.relative = data.relative;
        this.parent = data.parent;
        this.crumb = data.crumb || [];
        this.dirs = data.dirs || [];
      } catch (e) {
        this.error = 'Failed to load folders';
      } finally {
        this.loading = false;
      }
    },

    up() {
      if (this.parent) this.load(this.parent);
    },
    enter(dir) {
      this.load(dir.path);
    },

    select(dir) {
      this.selected =
        this.selected && this.selected.path === dir.path ? null : dir;
    },
    openSelected() {
      if (this.selected) this.enter(this.selected);
    },

    filteredDirs() {
      const q = this.filter.toLowerCase();
      return q
        ? this.dirs.filter((d) => d.name.toLowerCase().includes(q))
        : this.dirs;
    },

    keynav(e) {
      const items = this.filteredDirs();
      if (!items.length) return;
      const idx = this.selected
        ? items.findIndex((d) => d.path === this.selected.path)
        : -1;
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        this.selected = items[Math.min(idx + 1, items.length - 1)];
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        this.selected = items[Math.max(idx - 1, 0)];
      } else if (e.key === 'Enter') {
        e.preventDefault();
        this.openSelected();
      } else if (e.key === 'Backspace') {
        this.up();
      }
    },

    pick() {
      this.$dispatch('dirpick', {
        path: this.current,
        root: this.current_root,
        relative: this.relative,
      });
      this.open = false;
    },

    async create() {
      this.createError = '';
      const name = this.newName.trim();
      if (!name) return;
      const body = new URLSearchParams({ path: this.current, name });
      try {
        const res = await fetch('/browse/create', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
          },
          body: body.toString(),
        });
        const data = await res.json();
        if (!res.ok) {
          this.createError = data.error || 'Could not create folder';
          return;
        }
        this.newName = '';
        this.creating = false;
        this.load(this.current);
      } catch (e) {
        this.createError = 'Could not create folder';
      }
    },
  };
}
