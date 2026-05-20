// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

import React, { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import '../scss/App.scss';

import { useDispatch, useSelector } from "react-redux"; /* code change */
import { 
    DataFormulatorState,
    dfActions,
    dfSelectors,
} from '../app/dfSlice'

import _ from 'lodash';

import { Allotment, AllotmentHandle } from "allotment";
import "allotment/dist/style.css";

import {
    Typography,
    Box,
    Tooltip,
    Button,
    Divider,
    useTheme,
    alpha,
    CircularProgress,
    Backdrop,
    Link,
    Select,
    MenuItem,
    TextField,
} from '@mui/material';
import { borderColor, radius } from '../app/tokens';


import { VisualizationViewFC } from './VisualizationView';

import { DndProvider } from 'react-dnd'
import { HTML5Backend } from 'react-dnd-html5-backend'
import { toolName } from '../app/App';
import { DataThread } from './DataThread';

import dfLogo from '../assets/df-logo.png';
import exampleImageTable from "../assets/example-image-table.png";
import { ModelSelectionButton } from './ModelSelectionDialog';
import { UnifiedDataUploadDialog, UploadTabType, DataLoadMenu, ConnectorInstance } from './UnifiedDataUploadDialog';
import { ReportView } from './ReportView';
import { DataSourceSidebar } from './DataSourceSidebar';
import GitHubIcon from '@mui/icons-material/GitHub';
import { ExampleSession, exampleSessions, ExampleSessionCard, fetchExampleSessions } from './ExampleSessions';
import { useDataRefresh, useDerivedTableRefresh } from '../app/useDataRefresh';
import type { DictTable } from '../components/ComponentType';
import { useTranslation } from 'react-i18next';
import { fetchWithIdentity, getUrls, CONNECTOR_URLS } from '../app/utils';
import { apiRequest } from '../app/apiClient';
import { listWorkspaces, loadWorkspace, deleteWorkspace, exportWorkspace, importWorkspace, onWorkspaceListChanged, updateWorkspaceMeta } from '../app/workspaceService';
import type { WorkspaceSummary } from '../app/workspaceService';
import { AppDispatch } from '../app/store';
import { loadWorkspaceTableByName } from '../app/tableThunks';
import { generateUUID } from '../app/identity';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import IconButton from '@mui/material/IconButton';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import DownloadIcon from '@mui/icons-material/Download';
import UploadFileIcon from '@mui/icons-material/UploadFile';
import EditOutlinedIcon from '@mui/icons-material/EditOutlined';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import Dialog from '@mui/material/Dialog';
import DialogTitle from '@mui/material/DialogTitle';
import DialogContent from '@mui/material/DialogContent';
import DialogActions from '@mui/material/DialogActions';

/** Generate a session ID like session_20260408_193052_a1b2 */
function generateSessionId(): string {
    const now = new Date();
    const date = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, '0')}${String(now.getDate()).padStart(2, '0')}`;
    const time = `${String(now.getHours()).padStart(2, '0')}${String(now.getMinutes()).padStart(2, '0')}${String(now.getSeconds()).padStart(2, '0')}`;
    const short = generateUUID().slice(0, 4);
    return `session_${date}_${time}_${short}`;
}

export const DataFormulatorFC = ({ }) => {

    const tables = useSelector((state: DataFormulatorState) => state.tables);
    const activeWorkspace = useSelector((state: DataFormulatorState) => state.activeWorkspace);
    const focusedId = useSelector((state: DataFormulatorState) => state.focusedId);
    const models = useSelector(dfSelectors.getAllModels);
    const selectedModelId = useSelector((state: DataFormulatorState) => state.selectedModelId);
    const viewMode = useSelector((state: DataFormulatorState) => state.viewMode);
    const serverConfig = useSelector((state: DataFormulatorState) => state.serverConfig);
    const identityKey = useSelector((state: DataFormulatorState) => `${state.identity.type}:${state.identity.id}`);
    const theme = useTheme();

    const dispatch = useDispatch<AppDispatch>();
    const { t } = useTranslation();

    // Auto-focus: when focusedId is undefined but tables exist, select the first table
    useEffect(() => {
        if (!focusedId && tables.length > 0) {
            dispatch(dfActions.setFocused({ type: 'table', tableId: tables[0].id }));
        }
    }, [focusedId, tables, dispatch]);

    // ChatBI → DF 原生：消费 `/?ws=<id>&table=<name>` 把 chatbi 落到 workspace 的结果表挂进 store。
    // 用一次 IIFE 把"切 workspace → 加载表 → 清 URL"做成原子串行，避免靠 useEffect 重跑驱动状态机
    // 导致的 race（旧实现 step1 dispatch loadState 后偶尔不触发 step2，落到 tables.length===0 的
    // landing 主区，UI 误以为还要选数据源）。chatbiHandledRef 标记一次性消费。
    const chatbiHandledRef = useRef(false);
    useEffect(() => {
        if (chatbiHandledRef.current) return;
        if (typeof window === 'undefined') return;
        const params = new URLSearchParams(window.location.search);
        const wantWs = params.get('ws');
        const wantName = params.get('table');
        if (!wantName) return;
        chatbiHandledRef.current = true;

        (async () => {
            try {
                if (wantWs && (!activeWorkspace || activeWorkspace.id !== wantWs)) {
                    try {
                        const result = await loadWorkspace(wantWs);
                        if (result && Object.keys(result.state).length > 0) {
                            dispatch(dfActions.loadState({ ...result.state, activeWorkspace: { id: wantWs, displayName: result.displayName } }));
                        } else {
                            dispatch(dfActions.resetForNewWorkspace({ id: wantWs, displayName: wantWs }));
                        }
                    } catch (e) {
                        console.warn('[chatbi] 切换 workspace 失败:', wantWs, e);
                        dispatch(dfActions.resetForNewWorkspace({ id: wantWs, displayName: wantWs }));
                    }
                }
                await dispatch(loadWorkspaceTableByName(wantName))
                    .unwrap()
                    .catch((err) => {
                        console.warn('[chatbi] 加载结果表失败:', wantName, err);
                    });
            } finally {
                const url = new URL(window.location.href);
                url.searchParams.delete('ws');
                url.searchParams.delete('table');
                window.history.replaceState({}, '', url.toString());
            }
        })();
    }, [activeWorkspace, dispatch]);

    // ── Connector instances (for landing page menu) ─────────────
    const [pageConnectors, setPageConnectors] = useState<ConnectorInstance[]>([]);
    const refreshPageConnectors = useCallback(() => {
        apiRequest<any>(CONNECTOR_URLS.LIST, { method: 'GET' })
            .then(({ data }) => setPageConnectors(data.connectors || []))
            .catch(() => { /* connector list is optional on landing page */ });
    }, []);
    const [connectorRefreshKey, setConnectorRefreshKey] = useState(0);
    const handleConnectorsChanged = useCallback(() => {
        setConnectorRefreshKey(k => k + 1);
        refreshPageConnectors();
    }, [refreshPageConnectors]);
    useEffect(() => {
        setPageConnectors([]);
        refreshPageConnectors();
    }, [refreshPageConnectors, identityKey]);

    // ── Demo sessions (loaded from manifest, fallback to hardcoded) ─────
    const [demoSessions, setDemoSessions] = useState<ExampleSession[]>(exampleSessions);
    useEffect(() => {
        fetchExampleSessions().then(sessions => {
            if (sessions.length > 0) setDemoSessions(sessions);
        });
    }, []);

    // ── Workspace list (shown on landing page) ────────────────────
    const [savedWorkspaces, setSavedWorkspaces] = useState<WorkspaceSummary[]>([]);
    const [confirmDeleteWs, setConfirmDeleteWs] = useState<string | null>(null);

    // Inline rename: which card's title is currently being edited, and
    // its draft text. Persisted via updateWorkspaceMeta on Enter / blur;
    // reverted on Escape.
    const [renamingWs, setRenamingWs] = useState<string | null>(null);
    const [renameDraft, setRenameDraft] = useState<string>('');

    // Sort key for the saved-workspaces grid. Default is creation time
    // so the user's chronological list of work doesn't shuffle every
    // time a workspace is touched.
    type WsSortKey = 'created_desc' | 'created_asc' | 'updated_desc' | 'name_asc';
    const [wsSort, setWsSort] = useState<WsSortKey>('created_desc');

    const fetchWorkspaces = useCallback(async () => {
        try {
            const sessions = await listWorkspaces();
            setSavedWorkspaces(sessions);
        } catch { /* workspace list is best-effort on landing page */ }
    }, []);

    useEffect(() => {
        if (!activeWorkspace || tables.length === 0) {
            fetchWorkspaces();
        }
    }, [activeWorkspace, tables.length, fetchWorkspaces]);

    useEffect(() => {
        return onWorkspaceListChanged(fetchWorkspaces);
    }, [fetchWorkspaces]);

    const handleOpenWorkspace = useCallback(async (name: string) => {
        dispatch(dfActions.setSessionLoading({ loading: true, label: `Opening workspace...` }));
        try {
            const result = await loadWorkspace(name);
            if (result && Object.keys(result.state).length > 0) {
                dispatch(dfActions.loadState({ ...result.state, activeWorkspace: { id: name, displayName: result.displayName } }));
            } else {
                dispatch(dfActions.setActiveWorkspace({ id: name, displayName: 'Untitled Session' }));
            }
        } catch {
            dispatch(dfActions.setActiveWorkspace({ id: name, displayName: 'Untitled Session' }));
        }
        dispatch(dfActions.setSessionLoading({ loading: false }));
    }, [dispatch]);

    const handleDeleteWorkspace = useCallback(async (name: string) => {
        try {
            await deleteWorkspace(name);
            setSavedWorkspaces(prev => prev.filter(w => w.id !== name));
        } catch {
            dispatch(dfActions.addMessages({
                timestamp: Date.now(), type: 'error',
                component: 'workspace', value: 'Failed to delete workspace',
            }));
        }
        setConfirmDeleteWs(null);
    }, [dispatch]);

    const startRenameWorkspace = useCallback((id: string, currentName: string) => {
        setRenamingWs(id);
        setRenameDraft(currentName);
    }, []);

    const cancelRenameWorkspace = useCallback(() => {
        setRenamingWs(null);
        setRenameDraft('');
    }, []);

    const commitRenameWorkspace = useCallback(async () => {
        const id = renamingWs;
        if (!id) return;
        const next = renameDraft.trim();
        const current = savedWorkspaces.find(w => w.id === id);
        // Bail without writing if nothing changed or the new name is empty.
        if (!current || !next || next === current.display_name) {
            cancelRenameWorkspace();
            return;
        }
        // Optimistic update first so the UI reflects the change instantly;
        // the next list refresh (via onWorkspaceListChanged) will reconcile.
        setSavedWorkspaces(prev =>
            prev.map(w => (w.id === id ? { ...w, display_name: next } : w)),
        );
        cancelRenameWorkspace();
        try {
            await updateWorkspaceMeta(id, next);
        } catch {
            dispatch(dfActions.addMessages({
                timestamp: Date.now(), type: 'error',
                component: 'workspace', value: 'Failed to rename workspace',
            }));
            // On failure, refetch so the UI returns to the server's truth.
            fetchWorkspaces();
        }
    }, [renamingWs, renameDraft, savedWorkspaces, cancelRenameWorkspace, dispatch, fetchWorkspaces]);

    const handleExportWorkspace = useCallback(async (name: string) => {
        try {
            const blob = await exportWorkspace(name);
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = `${name}.zip`;
            a.click();
            URL.revokeObjectURL(a.href);
        } catch (e) {
            console.warn('Failed to export workspace:', e);
        }
    }, []);

    const importRef = useRef<HTMLInputElement>(null);
    const handleImportWorkspace = useCallback(async (event: React.ChangeEvent<HTMLInputElement>) => {
        const file = event.target.files?.[0];
        if (!file) return;
        dispatch(dfActions.setSessionLoading({ loading: true, label: `Importing ${file.name}...` }));
        try {
            const wsName = file.name.replace(/\.zip$/, '') || 'imported';
            const wsId = generateSessionId();
            const state = await importWorkspace(file, wsId, wsName);
            dispatch(dfActions.loadState({ ...state, activeWorkspace: { id: wsId, displayName: wsName } }));
        } catch (e) {
            console.warn('Failed to import workspace:', e);
        }
        dispatch(dfActions.setSessionLoading({ loading: false }));
        if (importRef.current) importRef.current.value = '';
    }, [dispatch]);

    // Sorted view of saved workspaces. We don't mutate the underlying
    // list (the backend's response is the source of truth); we just
    // produce a re-ordered copy for rendering.
    const sortedSavedWorkspaces = useMemo(() => {
        const cmpDate = (a: string | null | undefined, b: string | null | undefined): number => {
            // Missing timestamps sort last regardless of direction so
            // legacy entries don't dominate either end of the list.
            if (!a && !b) return 0;
            if (!a) return 1;
            if (!b) return -1;
            return a.localeCompare(b);
        };
        const copy = [...savedWorkspaces];
        switch (wsSort) {
            case 'created_desc':
                return copy.sort((a, b) => cmpDate(b.created_at, a.created_at));
            case 'created_asc':
                return copy.sort((a, b) => cmpDate(a.created_at, b.created_at));
            case 'updated_desc':
                return copy.sort((a, b) => cmpDate(b.saved_at, a.saved_at));
            case 'name_asc':
                return copy.sort((a, b) =>
                    (a.display_name || '').localeCompare(b.display_name || ''),
                );
            default:
                return copy;
        }
    }, [savedWorkspaces, wsSort]);
    
    // Set up automatic refresh of derived tables when source data changes
    useDerivedTableRefresh();

    // State for unified data upload dialog
    const [uploadDialogOpen, setUploadDialogOpen] = useState(false);
    const [uploadDialogInitialTab, setUploadDialogInitialTab] = useState<UploadTabType>('menu');

    // Loading state for sessions (from Redux, shared with App.tsx)
    const sessionLoading = useSelector((state: DataFormulatorState) => state.sessionLoading);
    const sessionLoadingLabel = useSelector((state: DataFormulatorState) => state.sessionLoadingLabel);

    const openUploadDialog = (tab: UploadTabType) => {
        // If no workspace is active, generate an ID (backend creates folder lazily on first data op)
        if (!activeWorkspace) {
            dispatch(dfActions.setActiveWorkspace({ id: generateSessionId(), displayName: 'Untitled Session' }));
        }
        setUploadDialogInitialTab(tab);
        setUploadDialogOpen(true);
    };

    const handleLoadExampleSession = async (session: ExampleSession) => {
        dispatch(dfActions.setSessionLoading({ loading: true, label: t('messages.loadingExample', { title: session.title }) }));

        dispatch(dfActions.addMessages({
            timestamp: Date.now(),
            type: 'info',
            component: 'data formulator',
            value: t('messages.loadingExample', { title: session.title }),
        }));

        try {
            // Fetch the workspace zip
            const res = await fetch(session.workspace);
            if (!res.ok) throw new Error(`Failed to fetch ${session.workspace}`);
            const blob = await res.blob();
            const file = new File([blob], `${session.id}.zip`, { type: 'application/zip' });

            // Import via the standard workspace import flow (parquet + state)
            const wsId = generateSessionId();
            // Set workspace ID first so fetchWithIdentity sends X-Workspace-Id header
            dispatch(dfActions.setActiveWorkspace({ id: wsId, displayName: session.title }));
            const state = await importWorkspace(file, wsId, session.title);
            dispatch(dfActions.loadState({ ...state, activeWorkspace: { id: wsId, displayName: session.title } }));

            dispatch(dfActions.addMessages({
                timestamp: Date.now(),
                type: 'success',
                component: 'data formulator',
                value: t('messages.loadSuccess', { title: session.title }),
            }));
        } catch (error: any) {
            console.error('Error loading session:', error);
            dispatch(dfActions.addMessages({
                timestamp: Date.now(),
                type: 'error',
                component: 'data formulator',
                value: t('messages.loadFailed', { title: session.title, error: error.message }),
            }));
        } finally {
            dispatch(dfActions.setSessionLoading({ loading: false }));
        }
    };

    useEffect(() => {
        document.title = toolName;
        
        // Preload imported images (public images are preloaded in index.html)
        const imagesToPreload = [
            { src: dfLogo, type: 'image/png' },
            { src: exampleImageTable, type: 'image/png' },
        ];
        
        const preloadLinks: HTMLLinkElement[] = [];
        imagesToPreload.forEach(({ src, type }) => {
            // Use link preload for better priority
            const link = document.createElement('link');
            link.rel = 'preload';
            link.as = 'image';
            link.href = src;
            link.type = type;
            document.head.appendChild(link);
            preloadLinks.push(link);
        });
        
        // Cleanup function to remove preload links when component unmounts
        return () => {
            preloadLinks.forEach(link => {
                if (link.parentNode) {
                    link.parentNode.removeChild(link);
                }
            });
        };
    }, []);

    useEffect(() => {
        // Auto-select the first available model when none is selected.
        // No connectivity check on load — errors surface on first use,
        // and the user can manually test via the model selection dialog.
        if (selectedModelId === undefined && models.length > 0) {
            dispatch(dfActions.selectModel(models[0].id));
        }
    }, [dispatch, models, selectedModelId]);

    const visPaneMain = (
        <Box sx={{ width: "100%", height: "100%", overflow: "hidden", display: "flex", flexDirection: "row" }}>
            <VisualizationViewFC />
        </Box>);

    const visPane = visPaneMain;

    let borderBoxStyle = {
        border: `1px solid ${borderColor.view}`, 
        borderRadius: radius.pill, 
        //boxShadow: '0 0 5px rgba(0,0,0,0.1)',
    }

    // Discrete column snapping for DataThread
    const CARD_WIDTH = 220;
    const CARD_GAP = 12;
    const COLUMN_WIDTH = CARD_WIDTH + CARD_GAP;
    const PANE_PADDING = 48;
    const columnSize = (n: number) => n * COLUMN_WIDTH + PANE_PADDING;
    const allotmentRef = useRef<AllotmentHandle>(null);
    const containerRef = useRef<HTMLDivElement>(null);

    const snapToColumns = useCallback((sizes: number[]) => {
        if (!allotmentRef.current || sizes.length < 2) return;
        const raw = sizes[0];
        // Find nearest discrete column count (1-3)
        let bestCols = 1;
        let bestDist = Infinity;
        for (let n = 1; n <= 3; n++) {
            const dist = Math.abs(raw - columnSize(n));
            if (dist < bestDist) {
                bestDist = dist;
                bestCols = n;
            }
        }
        const snapped = columnSize(bestCols);
        if (Math.abs(raw - snapped) > 2) {
            const totalWidth = sizes.reduce((a, b) => a + b, 0);
            allotmentRef.current.resize([snapped, totalWidth - snapped]);
        }
    }, []);

    // Compute thread count to decide preferred pane width:
    // A "thread" is a leaf table's derivation chain displayed as a column.
    // Must match the chain-splitting logic in DataThread (MAX_CHAIN_TABLES).
    const threadCount = useMemo(() => {
        // A table is a "leaf" if no other non-anchored table derives from it
        const hasNonAnchoredChild = new Set<string>();
        tables.forEach(t => {
            if (t.derive && !t.anchored) {
                hasNonAnchoredChild.add(t.derive.trigger.tableId);
            }
        });
        const leafTables = tables.filter(t => !hasNonAnchoredChild.has(t.id));
        // Threads = leaf tables with derivation chains + 1 group for hanging (source) tables
        const threaded = leafTables.filter(t => t.derive);
        const hanging = leafTables.filter(t => !t.derive);
        let count = threaded.length + (hanging.length > 0 ? 1 : 0);

        // Account for chain-splitting: long chains are broken into sub-threads
        // (mirrors MAX_CHAIN_TABLES logic in DataThread)
        const MAX_CHAIN_TABLES = 5;
        const tableById = new Map(tables.map(t => [t.id, t]));
        const getChainLength = (t: DictTable): number => {
            let len = 1;
            let cur = t;
            while (cur.derive && !cur.anchored) {
                len++;
                const parent = tableById.get(cur.derive.trigger.tableId);
                if (!parent) break;
                cur = parent;
            }
            return len;
        };
        const claimedForCount = new Set<string>();
        for (const lt of threaded) {
            // Walk chain
            const chainIds: string[] = [lt.id];
            let cur = lt;
            while (cur.derive && !cur.anchored) {
                const pid = cur.derive.trigger.tableId;
                chainIds.push(pid);
                const parent = tableById.get(pid);
                if (!parent) break;
                cur = parent;
            }
            const ownedIds = chainIds.filter(id => !claimedForCount.has(id));
            if (ownedIds.length > MAX_CHAIN_TABLES) {
                // Each extra split adds one more thread entry
                const extraSplits = Math.floor((ownedIds.length - 1) / MAX_CHAIN_TABLES);
                count += extraSplits;
            }
            chainIds.forEach(id => claimedForCount.add(id));
        }

        return count;
    }, [tables]);
    const preferredColumns = threadCount <= 1 ? 1 : 2;

    // Track previous thread count to auto-resize intelligently
    const prevThreadCountRef = useRef(threadCount);
    useEffect(() => {
        const prev = prevThreadCountRef.current;
        prevThreadCountRef.current = threadCount;
        if (!allotmentRef.current || !containerRef.current) return;
        // When there are no tables the first Allotment.Pane is unmounted,
        // so the Allotment only has one child – calling resize with two
        // sizes would crash (accessing .minimumSize on an undefined pane).
        if (tables.length === 0) return;
        const totalWidth = containerRef.current.clientWidth;
        if (totalWidth <= 0) return;

        let newSize: number | null = null;
        if (prev <= 1 && threadCount > 1) {
            // Case 1: was 1 thread, now 2+ → expand to 2 columns
            newSize = columnSize(2);
        } else if (prev > 1 && threadCount <= 1) {
            // Case 2: was 2+ threads, now 1 → shrink to 1 column
            newSize = columnSize(1);
        }
        // Case 3: was 2+ threads and still 2+ → don't change (respect user's manual setting)

        if (newSize !== null) {
            // Defer resize to the next animation frame so the Allotment has
            // re-rendered its pane children before we call resize.
            const finalSize = newSize;
            const rafId = requestAnimationFrame(() => {
                try {
                    const w = containerRef.current?.clientWidth ?? totalWidth;
                    allotmentRef.current?.resize([finalSize, w - finalSize]);
                } catch {
                    // Allotment pane structure may not yet match; ignore.
                }
            });
            return () => cancelAnimationFrame(rafId);
        }
    }, [threadCount, tables.length]);

    const fixedSplitPane = ( 
        <Box sx={{display: 'flex', flexDirection: 'row', height: '100%'}}>
            <DataSourceSidebar
                onOpenUploadDialog={(tab) => openUploadDialog((tab ?? 'add-connection') as UploadTabType)}
                connectorRefreshKey={connectorRefreshKey}
            />
            <Box ref={containerRef} className="outer-allotment" sx={{
                    margin: '4px 8px 8px 8px', backgroundColor: 'white',
                    display: 'flex', height: 'calc(100% - 12px)', flex: 1, minWidth: 0, flexDirection: 'column',
                    overflow: 'hidden',
                    position: 'relative'}}>
                <Allotment ref={allotmentRef} onDragEnd={snapToColumns} proportionalLayout={false}>
                    {tables.length > 0 ? (
                        <Allotment.Pane minSize={columnSize(1)} preferredSize={columnSize(preferredColumns)} maxSize={columnSize(3)} snap={false}>
                            <DataThread sx={{
                                display: 'flex', 
                                flexDirection: 'column',
                                overflow: 'hidden',
                                alignContent: 'flex-start',
                                height: '100%',
                            }}/>
                        </Allotment.Pane>
                    ) : null}
                    <Allotment.Pane minSize={300}>
                        <Box sx={{ ...borderBoxStyle, height: '100%', overflow: 'hidden', display: 'flex', flexDirection: 'column', boxSizing: 'border-box' }}>
                            {viewMode === 'editor' ? (
                                visPane
                            ) : (
                                <ReportView />
                            )}
                        </Box>
                    </Allotment.Pane>
                </Allotment>
            </Box>
        </Box>
    );

    // beelink 产品化：隐藏 Privacy / Terms / Contact / @year footer
    let footer = null

    let dataUploadRequestBox = <Box sx={{
            margin: '4px 4px 4px 8px', 
            background: `
                linear-gradient(90deg, ${alpha(theme.palette.text.secondary, 0.01)} 1px, transparent 1px),
                linear-gradient(0deg, ${alpha(theme.palette.text.secondary, 0.01)} 1px, transparent 1px)
            `,
            backgroundSize: '16px 16px',
            flex: 1, minWidth: 0, overflow: 'auto', display: 'flex', flexDirection: 'column', height: '100%',
        }}>
        <Box sx={{margin:'auto', pb: '5%', display: "flex", flexDirection: "column", textAlign: "center", maxWidth: 1024, width: '100%', px: 2, boxSizing: 'border-box' }}>
            {/* beelink 产品化：去掉 Data Formulator 大水印 + landing.tagline 副标题 */}
            <Box sx={{display: 'flex', mx: 'auto'}}>
                <Typography fontSize={48} sx={{ml: 2, letterSpacing: '0.05em', color: theme.palette.text.primary}}>{toolName}</Typography>
            </Box>

            {/* Hosted-demo notice — borderless strip (it's prose, not a
                button) placed before the Import Data section. The rocket
                gets a quiet lift to add a touch of life. */}
            {serverConfig.DISABLE_DATA_CONNECTORS && (
                <Box
                    sx={{
                        mt: 2,
                        mx: 'auto',
                        maxWidth: 760,
                        textAlign: 'left',
                        display: 'flex',
                        alignItems: 'center',
                        gap: 1.25,
                        px: 0.5,
                        py: 0.5,
                        // Sparkle emoji twinkle. Modern browsers' filter:
                        // drop-shadow honours the emoji's alpha channel,
                        // so a small-radius shadow hugs the actual glyph
                        // outline rather than a square box. We keep the
                        // radius tight (1–2px) and the alpha modest so
                        // the halo reads as a glow on the sparkle, not
                        // a rectangle behind it.
                        '& .df-sparkle': {
                            display: 'inline-block',
                            fontSize: 18,
                            lineHeight: 1,
                            animation: 'df-sparkle-twinkle 3.6s ease-in-out infinite',
                            transformOrigin: 'center',
                        },
                        '@keyframes df-sparkle-twinkle': {
                            '0%, 100%': {
                                transform: 'scale(1) rotate(0deg)',
                                filter: 'drop-shadow(0 0 0 rgba(255,200,80,0))',
                            },
                            '40%': {
                                transform: 'scale(1.2) rotate(20deg)',
                                filter: 'drop-shadow(0 0 2px rgba(255,200,80,0.85)) drop-shadow(0 0 1px rgba(255,180,40,0.6))',
                            },
                            '60%': {
                                transform: 'scale(1.05) rotate(-10deg)',
                                filter: 'drop-shadow(0 0 1px rgba(255,200,80,0.5))',
                            },
                        },
                    }}
                >
                    <Box
                        component="span"
                        className="df-sparkle"
                        role="img"
                        aria-label="sparkles"
                        sx={{ flexShrink: 0 }}
                    >
                        ✨
                    </Box>
                    <Typography
                        variant="caption"
                        sx={{ color: 'text.secondary', fontSize: 12.5, lineHeight: 1.5, flex: 1 }}
                    >
                        {t('landing.demoBannerBody', {
                            defaultValue:
                                'This is a demo site! Try the examples below or upload files. To work with large datasets, connect to databases, link local folders, create persisted analysis sessions, use custom models, and manage users, check the ',
                        })}
                        <Link
                            href="https://github.com/microsoft/data-formulator"
                            target="_blank"
                            rel="noopener noreferrer"
                            underline="hover"
                            sx={{
                                color: 'primary.main',
                                '&:hover': { color: 'primary.dark' },
                            }}
                        >
                            <GitHubIcon
                                sx={{
                                    fontSize: '1em',
                                    verticalAlign: '-0.15em',
                                    mr: 0.4,
                                }}
                            />
                            {t('landing.demoBannerCta', { defaultValue: 'installation guide' })}
                        </Link>
                        {t('landing.demoBannerSuffix', { defaultValue: '.' })}
                    </Typography>
                </Box>
            )}

            <Box sx={{mt: 4}}>
                <DataLoadMenu 
                    onSelectTab={(tab) => openUploadDialog(tab)}
                    onSelectConnector={(conn) => {
                        // Already-authed connector → open the data-source
                        // sidebar focused on it. Otherwise open the upload
                        // dialog at the connector's auth/connect tab.
                        if (conn.connected || conn.sso_auto_connect) {
                            dispatch(dfActions.focusConnector(conn.id));
                        } else {
                            openUploadDialog(`connector:${conn.id}` as UploadTabType);
                        }
                    }}
                    serverConfig={serverConfig}
                    variant="page"
                    connectors={pageConnectors}
                />
            </Box>

            {/* Demos — promoted ahead of "Your Sessions" on the hosted
                demo, since first-time visitors won't have any sessions
                yet and demos are the most engaging entry point. */}
            <Box sx={{mt: 4}}>
                <Divider sx={{width: '200px', mx: 'auto', mb: 3, fontSize: '1.2rem'}}>
                    <Typography sx={{ color: 'text.secondary' }}>
                        {t('landing.demos')}
                    </Typography>
                </Divider>
                <Box sx={{
                    display: 'grid',
                    gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))',
                    gap: 2,
                }}>
                    {demoSessions.map((session) => (
                        <ExampleSessionCard
                            key={session.id}
                            session={session}
                            onClick={() => handleLoadExampleSession(session)}
                        />
                    ))}
                </Box>
            </Box>

            {/* ── Saved workspaces section ──────────────────────────── */}
            <Box sx={{mt: 4}}>
                <Divider sx={{width: '200px', mx: 'auto', mb: 2, fontSize: '1.2rem'}}>
                    <Typography sx={{ color: 'text.secondary' }}>
                        {t('landing.yourSessions')}
                    </Typography>
                </Divider>
                {/* Sort control — placed in the upper-right of the section
                    so it's visible without competing with the divider title. */}
                <Box sx={{ display: 'flex', justifyContent: 'flex-end', mb: 1 }}>
                    <Select
                        size="small"
                        variant="standard"
                        value={wsSort}
                        onChange={(e) => setWsSort(e.target.value as typeof wsSort)}
                        disableUnderline
                        IconComponent={(props) => (
                            <ExpandMoreIcon {...props} sx={{ fontSize: 16, color: 'text.disabled', right: 0 }} />
                        )}
                        sx={{
                            fontSize: 12,
                            color: 'text.disabled',
                            cursor: 'pointer',
                            '& .MuiSelect-select': { py: 0.25, pl: 0, pr: '16px !important', minHeight: 0 },
                            '&:hover': { color: 'text.secondary' },
                            '&:hover .MuiSelect-icon': { color: 'text.secondary' },
                        }}
                        renderValue={(v) => {
                            const labels: Record<typeof wsSort, string> = {
                                created_desc: t('landing.sortNewest'),
                                created_asc: t('landing.sortOldest'),
                                updated_desc: t('landing.sortRecentlyModified'),
                                name_asc: t('landing.sortName'),
                            };
                            return labels[v as typeof wsSort];
                        }}
                    >
                        <MenuItem value="created_desc" sx={{ fontSize: 12 }}>{t('landing.sortNewestFirst')}</MenuItem>
                        <MenuItem value="created_asc" sx={{ fontSize: 12 }}>{t('landing.sortOldestFirst')}</MenuItem>
                        <MenuItem value="updated_desc" sx={{ fontSize: 12 }}>{t('landing.sortRecentlyModified')}</MenuItem>
                        <MenuItem value="name_asc" sx={{ fontSize: 12 }}>{t('landing.sortNameAZ')}</MenuItem>
                    </Select>
                </Box>
                <Box sx={{
                    display: 'grid',
                    gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))',
                    gap: 2,
                }}>
                    {sortedSavedWorkspaces.map(w => {
                        const isRenaming = renamingWs === w.id;
                        return (
                        <Card key={w.id} variant="outlined" onClick={isRenaming ? undefined : () => handleOpenWorkspace(w.id)} sx={{
                            position: 'relative', textAlign: 'left',
                            cursor: isRenaming ? 'default' : 'pointer',
                            '&:hover': isRenaming ? {} : { transform: 'translateY(-2px)', backgroundColor: 'action.hover' },
                            '&:hover .ws-actions': { opacity: 1 },
                        }}>
                            <CardContent sx={{ py: 1.5, px: 2 }}>
                                {isRenaming ? (
                                    <TextField
                                        autoFocus
                                        fullWidth
                                        variant="standard"
                                        value={renameDraft}
                                        onChange={(e) => setRenameDraft(e.target.value)}
                                        onClick={(e) => e.stopPropagation()}
                                        onBlur={commitRenameWorkspace}
                                        onKeyDown={(e) => {
                                            if (e.key === 'Enter') {
                                                e.preventDefault();
                                                commitRenameWorkspace();
                                            } else if (e.key === 'Escape') {
                                                e.preventDefault();
                                                cancelRenameWorkspace();
                                            }
                                        }}
                                        slotProps={{ input: { sx: { fontSize: 14, fontWeight: 500 } } }}
                                    />
                                ) : (
                                    <Typography variant="body2" fontWeight={500} noWrap sx={{ color: 'text.primary' }}>
                                        {w.display_name}
                                    </Typography>
                                )}
                                {w.saved_at && (
                                    <Typography variant="caption" color="text.disabled" sx={{ fontSize: 11 }}>
                                        {new Date(w.saved_at).toLocaleString()}
                                    </Typography>
                                )}
                            </CardContent>
                            <Box className="ws-actions" sx={{
                                position: 'absolute', top: 4, right: 4,
                                display: isRenaming ? 'none' : 'flex',
                                gap: 0.25,
                                opacity: 0,
                                transition: 'opacity 0.15s',
                            }}>
                                <Tooltip title={t('landing.tooltipRename')}>
                                    <IconButton size="small" sx={{ color: 'text.secondary', backgroundColor: 'rgba(255,255,255,0.85)', '&:hover': { backgroundColor: 'rgba(240,240,240,0.95)' } }}
                                        onClick={(e) => { e.stopPropagation(); startRenameWorkspace(w.id, w.display_name); }}>
                                        <EditOutlinedIcon fontSize="small" />
                                    </IconButton>
                                </Tooltip>
                                <Tooltip title={t('landing.tooltipExport')}>
                                    <IconButton size="small" sx={{ color: 'text.secondary', backgroundColor: 'rgba(255,255,255,0.85)', '&:hover': { backgroundColor: 'rgba(240,240,240,0.95)' } }}
                                        onClick={(e) => { e.stopPropagation(); handleExportWorkspace(w.id); }}>
                                        <DownloadIcon fontSize="small" />
                                    </IconButton>
                                </Tooltip>
                                <Tooltip title={t('landing.tooltipDelete')}>
                                    <IconButton size="small" sx={{ color: 'text.secondary', backgroundColor: 'rgba(255,255,255,0.85)', '&:hover': { backgroundColor: 'rgba(240,240,240,0.95)' } }}
                                        onClick={(e) => { e.stopPropagation(); setConfirmDeleteWs(w.id); }}>
                                        <DeleteOutlineIcon fontSize="small" />
                                    </IconButton>
                                </Tooltip>
                            </Box>
                        </Card>
                        );
                    })}
                    {/* Import workspace card */}
                    <Card variant="outlined" onClick={() => importRef.current?.click()} sx={{
                        textAlign: 'center', borderStyle: 'dashed',
                        cursor: 'pointer',
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        gap: 1, px: 2, py: 1.5,
                        '&:hover': { transform: 'translateY(-2px)', backgroundColor: 'action.hover' },
                    }}>
                        <UploadFileIcon sx={{ color: 'text.secondary', fontSize: 20 }} />
                        <Typography variant="caption" color="text.secondary">{t('landing.importWorkspaceZip')}</Typography>
                        <input type="file" hidden accept=".zip" ref={importRef} onChange={handleImportWorkspace} />
                    </Card>
                </Box>
            </Box>
            {/* ── Delete workspace confirmation ────────────────────── */}
            <Dialog open={confirmDeleteWs !== null} onClose={() => setConfirmDeleteWs(null)}>
                <DialogTitle>{t('landing.deleteSessionTitle')}</DialogTitle>
                <DialogContent>
                    <Typography>
                        {t('landing.deleteSessionDesc', {
                            name: savedWorkspaces.find(w => w.id === confirmDeleteWs)?.display_name || confirmDeleteWs,
                            id: confirmDeleteWs,
                        })}
                    </Typography>
                </DialogContent>
                <DialogActions>
                    <Button onClick={() => setConfirmDeleteWs(null)}>{t('app.cancel')}</Button>
                    <Button color="error" onClick={() => confirmDeleteWs && handleDeleteWorkspace(confirmDeleteWs)}>
                        {t('app.delete')}
                    </Button>
                </DialogActions>
            </Dialog>
        </Box>
        {footer}
    </Box>;
    
    return (
        <Box sx={{ display: 'block', width: "100%", height: '100%', position: 'relative' }}>
            <DndProvider backend={HTML5Backend}>
                {tables.length > 0 ? fixedSplitPane : (
                    <Box sx={{ display: 'flex', flexDirection: 'row', height: '100%' }}>
                        <DataSourceSidebar
                            onOpenUploadDialog={(tab) => openUploadDialog((tab ?? 'add-connection') as UploadTabType)}
                            connectorRefreshKey={connectorRefreshKey}
                        />
                        {dataUploadRequestBox}
                    </Box>
                )}
                <UnifiedDataUploadDialog 
                    open={uploadDialogOpen}
                    onClose={() => { setUploadDialogOpen(false); refreshPageConnectors(); }}
                    initialTab={uploadDialogInitialTab}
                    onConnectorsChanged={handleConnectorsChanged}
                />
                {/* Loading overlay for session loading */}
                <Backdrop
                    open={sessionLoading}
                    sx={{
                        position: 'absolute',
                        zIndex: 999,
                        backgroundColor: alpha(theme.palette.background.default, 0.85),
                        backdropFilter: 'blur(4px)',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: 2,
                    }}
                >
                    <CircularProgress size={40} />
                    <Typography variant="body1" color="text.secondary">
                        {sessionLoadingLabel || t('session.loadingSessions')}
                    </Typography>
                    <Button
                        variant="text"
                        size="small"
                        onClick={() => dispatch(dfActions.setSessionLoading({ loading: false }))}
                        sx={{ mt: 1, textTransform: 'none', color: 'text.secondary' }}
                    >
                        {t('app.cancel')}
                    </Button>
                </Backdrop>
                {/* beelink 产品化：没选 model 时显示简洁提示（不再用大水印 backdrop 阻挡视图）。
                    服务端注入 DEEPSEEK_API_KEY 后，list-global-models 会自动注册 deepseek-chat，
                    dfSlice.tsx:1611 会把 selectedModelId 自动设到第一个全局模型，所以正常进入时
                    这层根本不渲染；仅在用户主动清除 selection 时才作为一行小提示出现。 */}
                {selectedModelId == undefined && (
                    <Box sx={{
                        position: 'absolute',
                        top: 0, left: 0, right: 0, bottom: 0,
                        backgroundColor: alpha(theme.palette.background.default, 0.6),
                        display: 'flex', alignItems: 'flex-start', justifyContent: 'center',
                        pointerEvents: 'none',
                        zIndex: 1000,
                    }}>
                        <Box sx={{mt: 6, pointerEvents: 'auto', backgroundColor: theme.palette.background.paper, px: 2, py: 1, borderRadius: 1, boxShadow: 1, display: 'flex', alignItems: 'center', gap: 1}}>
                            <Typography variant="body2">{t('landing.firstSelectModelPrefix')}</Typography>
                            <ModelSelectionButton />
                        </Box>
                    </Box>
                )}
            </DndProvider>
        </Box>);
}