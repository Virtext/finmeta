import React, { useState } from 'react';
import {
  Button, IconButton, Input, Textarea, Select, NumberField, Switch, Checkbox,
  Card, CardHeader, CardBody, CardFooter, Table, Badge, StatusDot, Tabs,
  Dialog, Drawer, Menu, Tooltip, ToastProvider, useToast, Skeleton, EmptyState,
  ErrorState, PageHeader, PosterCard, Icon,
} from './index';

// Dev-only primitives gallery (SPEC_S14_UI.md §6, phase P1). Reached only via
// #/_ds -- see main.tsx. Never linked from the real app, never shipped as a
// nav entry. Exercises every primitive with real interaction, not just a
// static render, so a reviewer can actually click things before any page
// is built on top of them.

interface Row { id: string; name: string; status: 'ok' | 'warn' | 'danger'; count: number; }

const ROWS: Row[] = [
  { id: '1', name: 'Movies', status: 'ok', count: 412 },
  { id: '2', name: 'Shows', status: 'warn', count: 88 },
  { id: '3', name: 'Anime', status: 'danger', count: 4 },
];

function ToastDemo(): React.ReactElement {
  const { push } = useToast();
  return (
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
      <Button size="sm" onClick={() => push({ message: 'Applied 214 files to Movies', actionLabel: 'Undo', onAction: () => push({ message: 'Undone', tone: 'ok' }) })}>
        Success toast
      </Button>
      <Button size="sm" onClick={() => push({ message: 'Scan found 3 errors', tone: 'danger' })}>Error toast</Button>
      <Button size="sm" onClick={() => push({ message: 'MDBList key missing', tone: 'warn' })}>Warning toast</Button>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }): React.ReactElement {
  return (
    <section style={{ marginBottom: 40 }}>
      <h2 style={{ fontSize: 13, fontFamily: 'var(--ui-mono)', letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--text-faint)', borderBottom: '1px solid var(--border-subtle)', paddingBottom: 10, marginBottom: 16 }}>
        {title}
      </h2>
      {children}
    </section>
  );
}

function GalleryInner(): React.ReactElement {
  const [theme, setTheme] = useState<'dark' | 'light'>('dark');
  const [switchOn, setSwitchOn] = useState(true);
  const [checked, setChecked] = useState(false);
  const [tab, setTab] = useState('a');
  const [dialogOpen, setDialogOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selected, setSelected] = useState<string | null>('2');
  const [posterSel, setPosterSel] = useState(true);
  const [num, setNum] = useState(24);

  return (
    <div className="fm-modern" data-ui="modern" data-theme={theme} style={{ background: 'var(--canvas)', minHeight: '100vh', padding: '28px 32px' }}>
      <PageHeader
        crumb="Dev only · not linked from the app"
        title="Primitive gallery"
        description="SPEC_S14_UI.md §2.5 — every component in ui/src/ui/, reachable only via #/_ds."
        actions={
          <>
            <IconButton icon={theme === 'dark' ? 'sun' : 'moon'} label="Toggle theme" onClick={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))} />
            <Button variant="primary" onClick={() => setDialogOpen(true)}>Open dialog</Button>
          </>
        }
      />

      <Section title="Buttons">
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
          <Button>Default</Button>
          <Button variant="primary">Primary</Button>
          <Button variant="ghost">Ghost</Button>
          <Button variant="danger">Danger</Button>
          <Button loading>Loading</Button>
          <Button disabled>Disabled</Button>
          <Button size="sm">Small</Button>
          <IconButton icon="undo" label="Undo last change" />
          <IconButton icon="bell" label="Notifications" dot />
        </div>
      </Section>

      <Section title="Form fields">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 16, maxWidth: 760 }}>
          <Input label="Jellyfin URL" placeholder="http://192.168.1.x:8096" help="Reachable from inside the Finmeta container." />
          <Select label="Preferred language" options={[{ value: 'en', label: 'English (en)' }, { value: 'ja', label: 'Japanese (ja)' }]} defaultValue="en" />
          <NumberField label="Sample size" value={num} onChange={setNum} min={1} max={20} suffix="items" />
          <Input label="With an error" defaultValue="not-a-url" error="Must start with http:// or https://" />
        </div>
        <div style={{ marginTop: 14, maxWidth: 420 }}>
          <Textarea label="Raw YAML override" defaultValue={'overlays:\n  elements: []'} />
        </div>
        <div style={{ display: 'flex', gap: 24, marginTop: 16, alignItems: 'center' }}>
          <label style={{ display: 'flex', gap: 9, alignItems: 'center', fontSize: 13 }}>
            <Switch checked={switchOn} onChange={setSwitchOn} label="Dry run" /> Dry run
          </label>
          <label style={{ display: 'flex', gap: 9, alignItems: 'center', fontSize: 13 }}>
            <Checkbox checked={checked} onChange={setChecked} label="Select row" /> Select row
          </label>
        </div>
      </Section>

      <Section title="Card, Badge, StatusDot">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 14 }}>
          <Card>
            <CardHeader title="Artwork coverage" action={<a href="#" style={{ fontSize: 12, color: 'var(--violet)' }}>Health ›</a>} />
            <CardBody>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
                <Badge tone="ok">Complete 1042</Badge>
                <Badge tone="warn">Missing logo 88</Badge>
                <Badge tone="danger">No poster 31</Badge>
                <Badge tone="accent">now</Badge>
              </div>
              <div style={{ display: 'flex', gap: 8, fontSize: 13, color: 'var(--text-muted)' }}>
                <StatusDot tone="ok" /> Jellyfin
                <StatusDot tone="warn" /> MDBList
                <StatusDot tone="danger" /> Sonarr
              </div>
            </CardBody>
            <CardFooter>
              <Button size="sm">Rescan</Button>
              <Button size="sm" variant="ghost">Dismiss</Button>
            </CardFooter>
          </Card>
        </div>
      </Section>

      <Section title="Table (selectable rows)">
        <Table<Row>
          columns={[
            { key: 'name', header: 'Library', render: (r) => r.name },
            { key: 'status', header: 'Status', render: (r) => <StatusDot tone={r.status} title={r.status} /> },
            { key: 'count', header: 'Items', render: (r) => r.count },
          ]}
          rows={ROWS}
          rowKey={(r) => r.id}
          selectedKey={selected}
          onRowClick={(r) => setSelected(r.id)}
        />
      </Section>

      <Section title="Tabs (arrow-key roving tabindex)">
        <Tabs
          aria-label="Demo tabs"
          items={[{ id: 'a', label: 'Elements' }, { id: 'b', label: 'Advanced' }, { id: 'c', label: 'Format rules' }]}
          activeId={tab}
          onChange={setTab}
        />
        <div style={{ padding: '14px 2px', fontSize: 13, color: 'var(--text-muted)' }}>Active tab: {tab}</div>
      </Section>

      <Section title="Menu, Tooltip">
        <div style={{ display: 'flex', gap: 20, alignItems: 'center' }}>
          <Menu
            trigger={<Button variant="ghost">Actions ▾</Button>}
            items={[
              { id: 'plan', label: 'Plan library', onSelect: () => {} },
              { id: 'apply', label: 'Apply library', onSelect: () => {} },
              { separator: true },
              { id: 'reset', label: 'Reset to default', danger: true, onSelect: () => {} },
            ]}
          />
          <Tooltip text="Finmeta prefers this candidate, then falls back to the highest rated.">
            <span style={{ display: 'inline-flex', cursor: 'help' }}><Icon name="warning" size={15} /></span>
          </Tooltip>
        </div>
      </Section>

      <Section title="Toast">
        <ToastDemo />
      </Section>

      <Section title="Skeleton, empty &amp; error states">
        <div style={{ display: 'flex', gap: 10, marginBottom: 20, maxWidth: 400 }}>
          <Skeleton width={46} height={66} radius="var(--r-md)" />
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 8 }}>
            <Skeleton height={14} width="60%" />
            <Skeleton height={12} width="40%" />
          </div>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
          <Card><EmptyState icon="search" title="No titles match" description='Try a different search term, or clear the filter.' /></Card>
          <Card><ErrorState title="Could not reach Jellyfin" description="Check the server URL in Settings → Jellyfin." action={<Button size="sm">Retry</Button>} /></Card>
        </div>
      </Section>

      <Section title="PosterCard">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(130px, 1fr))', gap: 14, maxWidth: 560 }}>
          <PosterCard title="Arrival" meta="2016 · 1080p" selectable selected={posterSel} onToggleSelect={() => setPosterSel((v) => !v)} marks={[{ text: 'NO LOGO', tone: 'warn' }]} badges={['1080', 'DTS']} />
          <PosterCard title="Interstellar" meta="2014 · locked" locked badges={['4K', 'ATMOS']} />
          <PosterCard title="Stalker" meta="1979 · needs id" marks={[{ text: 'NO MATCH', tone: 'danger' }]} />
        </div>
      </Section>

      <Dialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        title="Apply ALL configured libraries?"
        actions={<>
          <Button variant="ghost" onClick={() => setDialogOpen(false)}>Cancel</Button>
          <Button variant="primary" onClick={() => setDialogOpen(false)}>Apply all libraries</Button>
        </>}
      >
        This replaces the old <code>window.confirm</code>. A real implementation shows the plan
        summary here before you commit — Movies: 214 changes, Shows: 12 changes, Anime: 0 changes.
      </Dialog>

      <div style={{ marginTop: 20 }}>
        <Button onClick={() => setDrawerOpen(true)}>Open drawer</Button>
      </div>
      <Drawer open={drawerOpen} onClose={() => setDrawerOpen(false)} title="Item detail (example)">
        <h3 style={{ margin: '0 0 6px', fontSize: 18 }}>Blade Runner 2049</h3>
        <p style={{ color: 'var(--text-muted)', fontSize: 13 }}>Drawer primitive demo — focus-trapped, Esc closes, scroll locked behind it.</p>
        <Button size="sm" variant="ghost" onClick={() => setDrawerOpen(false)}>Close</Button>
      </Drawer>
    </div>
  );
}

export function Gallery(): React.ReactElement {
  return (
    <ToastProvider>
      <GalleryInner />
    </ToastProvider>
  );
}

export default Gallery;
