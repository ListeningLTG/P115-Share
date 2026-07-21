<template>
  <div class="sensitive-container">
    <a-typography-title :level="4" style="margin-bottom: 24px">敏感词管理</a-typography-title>

    <a-tabs v-model:activeKey="activeTab">
      <!-- 选项卡 1：敏感词替换 -->
      <a-tab-pane key="replace" tab="敏感词替换">
        <div style="padding-top: 16px">
          <a-alert
            message="功能说明"
            description="此处开关仅对 TG 机器人接收并自动转存分享的链接生效。对于定时分享任务或批量转存分享任务，请在各自任务的配置中单独开启和设置敏感词替换与拼音全拼替换规则。这里的敏感词映射表是全局共享的，所有启用敏感词替换的任务都会使用本映射表。"
            type="info"
            show-icon
            style="margin-bottom: 24px"
          />

          <a-card title="替换规则配置">
            <a-form layout="vertical">
              <a-form-item label="启用敏感词替换">
                <a-switch v-model:checked="replaceEnabled" />
              </a-form-item>

              <a-form-item label="启用中文名称替换为拼音全拼">
                <template #extra>
                  <div style="font-size: 12px; color: #999; margin-top: 4px">
                    开启后，生成分享前将文件和目录名中的中文替换为拼音全拼（如「斗破苍穹」→「Duo Po Cang Qiong」）。优先级最低（映射表、TMDB 均未命中时才生效）。仅在启用敏感词替换时生效。
                  </div>
                </template>
                <a-switch v-model:checked="replacePinyin" :disabled="!replaceEnabled" />
              </a-form-item>

              <a-form-item label="启用 TMDB 别名替换">
                <template #extra>
                  <div style="font-size: 12px; color: #999; margin-top: 4px">
                    开启后，若中文名称的文件或目录具有 TMDB ID 标识，使用 TMDB 英文/原版别名替换其中文名称。优先级次于敏感词映射表。仅在启用敏感词替换时生效。
                  </div>
                </template>
                <a-tooltip :title="tmdbDisabledTip">
                  <span style="display: inline-block">
                    <a-switch
                      v-model:checked="replaceTmdb"
                      :disabled="!replaceEnabled || !isTmdbConfigured"
                    />
                  </span>
                </a-tooltip>
              </a-form-item>

              <a-form-item label="敏感词映射表 (JSON 格式)">
                <template #extra>
                  <div style="font-size: 12px; color: #999; margin-top: 4px">
                    请输入一个标准的 JSON 对象格式的映射表。键为需要被替换的敏感词，值为替换后的词。命中映射表时优先级最高，不再走 TMDB / 拼音替换。
                    <br />
                    示例：<code>{"斗破苍穹": "dpcq", "凡人修仙传": "frxxz", "违规词": "xxx"}</code>
                  </div>
                </template>
                <a-textarea
                  v-model:value="replaceMappingStr"
                  placeholder='{"斗破苍穹": "dpcq", "凡人修仙传": "frxxz"}'
                  :rows="10"
                  style="font-family: monospace; font-size: 13px"
                />
              </a-form-item>

              <a-space>
                <a-button type="primary" @click="saveReplaceConfig" :loading="savingReplace">
                  保存配置
                </a-button>
                <a-button @click="validateMappingJson">
                  校验 JSON 格式
                </a-button>
              </a-space>
            </a-form>
          </a-card>
        </div>
      </a-tab-pane>

      <a-tab-pane key="tmdb_cache" tab="TMDB别名库">
        <div style="padding-top: 16px">
          <a-alert
            message="功能说明"
            description="用于缓存和维护 TMDB 别名结果。命中缓存后会跳过实时 TMDB 请求以减少耗时和网络依赖。"
            type="info"
            show-icon
            style="margin-bottom: 16px"
          />

          <a-card>
            <a-space style="margin-bottom: 16px; width: 100%; justify-content: space-between">
              <a-space>
                <a-input-search
                  v-model:value="cacheSearch"
                  placeholder="搜索 tmdb_id/中文名/原名/别名"
                  style="width: 320px"
                  @search="fetchTmdbCache(1)"
                />
                <a-select v-model:value="cacheStatus" style="width: 140px" @change="fetchTmdbCache(1)">
                  <a-select-option value="">全部状态</a-select-option>
                  <a-select-option value="success">success</a-select-option>
                  <a-select-option value="failed">failed</a-select-option>
                </a-select>
              </a-space>
              <a-space>
                <a-button @click="fetchTmdbCache(cachePage)">刷新</a-button>
                <a-popconfirm title="确认删除选中的缓存记录？" ok-text="删除" cancel-text="取消" @confirm="batchDeleteCacheRecords">
                  <a-button danger :disabled="selectedCacheRowKeys.length === 0">批量删除</a-button>
                </a-popconfirm>
                <a-button type="primary" @click="openCreateModal">新增记录</a-button>
              </a-space>
            </a-space>

            <a-table
              :columns="cacheColumns"
              :data-source="cacheItems"
              :loading="cacheLoading"
              :pagination="false"
              :row-selection="cacheRowSelection"
              row-key="id"
              size="small"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'status'">
                  <a-tag :color="record.status === 'success' ? 'green' : 'orange'">{{ record.status || '-' }}</a-tag>
                </template>
                <template v-else-if="column.key === 'actions'">
                  <a-button type="link" size="small" @click="openEditModal(record)">编辑</a-button>
                  <a-popconfirm title="确认删除该缓存记录？" ok-text="删除" cancel-text="取消" @confirm="deleteCacheRecord(record)">
                    <a-button type="link" size="small" danger>删除</a-button>
                  </a-popconfirm>
                </template>
              </template>
            </a-table>

            <div style="margin-top: 16px; text-align: right">
              <a-pagination
                :current="cachePage"
                :page-size="cachePageSize"
                :total="cacheTotal"
                :show-size-changer="true"
                :page-size-options="['10', '20', '50', '100']"
                @change="onCachePageChange"
                @showSizeChange="onCachePageSizeChange"
              />
            </div>
          </a-card>
        </div>
      </a-tab-pane>
    </a-tabs>

    <a-modal
      v-model:open="cacheModalVisible"
      :title="cacheEditId ? '编辑 TMDB 缓存' : '新增 TMDB 缓存'"
      :confirm-loading="cacheSaving"
      @ok="saveCacheRecord"
      ok-text="保存"
      cancel-text="取消"
    >
      <a-form layout="vertical">
        <a-form-item label="TMDB ID">
          <a-input-number v-model:value="cacheForm.tmdb_id" :min="1" style="width: 100%" :disabled="!!cacheEditId" />
        </a-form-item>
        <a-form-item label="媒体类型">
          <a-select v-model:value="cacheForm.media_type">
            <a-select-option value="unknown">unknown</a-select-option>
            <a-select-option value="movie">movie</a-select-option>
            <a-select-option value="tv">tv</a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item label="中文名">
          <a-input v-model:value="cacheForm.chinese_title" />
        </a-form-item>
        <a-form-item label="原名">
          <a-input v-model:value="cacheForm.original_title" />
        </a-form-item>
        <a-form-item label="别名">
          <a-input v-model:value="cacheForm.alias" />
        </a-form-item>
        <a-form-item label="来源">
          <a-input v-model:value="cacheForm.source" />
        </a-form-item>
        <a-form-item label="状态">
          <a-select v-model:value="cacheForm.status">
            <a-select-option value="success">success</a-select-option>
            <a-select-option value="failed">failed</a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item label="备注">
          <a-textarea v-model:value="cacheForm.note" :rows="3" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, computed, onMounted } from 'vue';
import axios from 'axios';
import { message } from 'ant-design-vue';

// Tab Key
const activeTab = ref('replace');

// Sensitive Replace State
const replaceEnabled = ref(false);
const replaceMappingStr = ref('{}');
const replacePinyin = ref(false);
const replaceTmdb = ref(false);
const savingReplace = ref(false);
const isTmdbConfigured = ref(false);
const tmdbDisabledTip = computed(() => {
  if (!isTmdbConfigured.value) {
    return '请先在系统配置中填写 TMDB API Key';
  }
  if (!replaceEnabled.value) {
    return '请先启用敏感词替换';
  }
  return '';
});

interface CacheItem {
  id: number;
  tmdb_id: number;
  media_type: string;
  chinese_title: string;
  original_title: string;
  alias: string;
  source: string;
  status: string;
  note: string;
  updated_at: string;
}

const cacheLoading = ref(false);
const cacheSaving = ref(false);
const cacheItems = ref<CacheItem[]>([]);
const cacheTotal = ref(0);
const cachePage = ref(1);
const cachePageSize = ref(20);
const cacheSearch = ref('');
const cacheStatus = ref('');
const cacheModalVisible = ref(false);
const cacheEditId = ref<number | null>(null);
const selectedCacheRowKeys = ref<number[]>([]);

const cacheForm = reactive({
  tmdb_id: undefined as number | undefined,
  media_type: 'unknown',
  chinese_title: '',
  original_title: '',
  alias: '',
  source: 'manual',
  status: 'success',
  note: ''
});

const cacheColumns = [
  { title: 'TMDB ID', dataIndex: 'tmdb_id', key: 'tmdb_id', width: 110 },
  { title: '类型', dataIndex: 'media_type', key: 'media_type', width: 90 },
  { title: '中文名', dataIndex: 'chinese_title', key: 'chinese_title', ellipsis: true },
  { title: '原名', dataIndex: 'original_title', key: 'original_title', ellipsis: true },
  { title: '别名', dataIndex: 'alias', key: 'alias', ellipsis: true },
  { title: '来源', dataIndex: 'source', key: 'source', width: 120 },
  { title: '状态', dataIndex: 'status', key: 'status', width: 100 },
  { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at', width: 180 },
  { title: '操作', key: 'actions', width: 130 }
];

const cacheRowSelection = {
  selectedRowKeys: selectedCacheRowKeys,
  onChange: (keys: (string | number)[]) => {
    selectedCacheRowKeys.value = keys.map((k) => Number(k));
  }
};

// Load sensitive replace config
const loadReplaceConfig = async () => {
  try {
    const res = await axios.get('/api/config/');
    replaceEnabled.value = res.data.sensitive_replace_enabled || false;
    replaceMappingStr.value = res.data.sensitive_replace_mapping || '{}';
    replacePinyin.value = res.data.sensitive_replace_pinyin || false;
    isTmdbConfigured.value = !!res.data.tmdb_api_key;
    replaceTmdb.value = isTmdbConfigured.value ? (res.data.sensitive_replace_tmdb || false) : false;
  } catch (e) {
    console.error('加载敏感词替换配置失败:', e);
    message.error('加载替换配置失败');
  }
};

// Validate JSON syntax and structure
const validateMappingJson = () => {
  try {
    const parsed = JSON.parse(replaceMappingStr.value);
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      message.error('映射表必须是一个 JSON 对象 (例如: {"敏感词": "替换词"})');
      return false;
    }
    for (const [k, v] of Object.entries(parsed)) {
      if (typeof k !== 'string' || typeof v !== 'string') {
        message.warning('映射表的键和值都应为字符串格式');
        return true;
      }
    }
    message.success('JSON 校验通过！格式正确。');
    return true;
  } catch (err: any) {
    message.error(`JSON 语法解析错误: ${err.message}`);
    return false;
  }
};

// Save sensitive replace config
const saveReplaceConfig = async () => {
  if (!validateMappingJson()) {
    return;
  }
  try {
    savingReplace.value = true;
    const res = await axios.post('/api/config/update', {
      sensitive_replace_enabled: replaceEnabled.value,
      sensitive_replace_mapping: replaceMappingStr.value,
      sensitive_replace_pinyin: replacePinyin.value,
      sensitive_replace_tmdb: isTmdbConfigured.value ? replaceTmdb.value : false
    });
    if (res.data.status === 'success') {
      message.success('替换配置已保存');
    } else {
      message.error(res.data.message || '保存失败');
    }
  } catch (e: any) {
    message.error(e.response?.data?.detail || '保存失败');
  } finally {
    savingReplace.value = false;
  }
};

const fetchTmdbCache = async (page = cachePage.value) => {
  try {
    cacheLoading.value = true;
    const res = await axios.get('/api/sensitive/tmdb-alias-cache', {
      params: {
        page,
        page_size: cachePageSize.value,
        search: cacheSearch.value,
        status: cacheStatus.value
      }
    });
    if (res.data?.state) {
      cacheItems.value = res.data.items || [];
      cacheTotal.value = res.data.total || 0;
      cachePage.value = res.data.page || page;
      const ids = new Set(cacheItems.value.map((item) => item.id));
      selectedCacheRowKeys.value = selectedCacheRowKeys.value.filter((id) => ids.has(id));
    }
  } catch (e) {
    console.error('加载 TMDB 缓存失败:', e);
    message.error('加载 TMDB 缓存失败');
  } finally {
    cacheLoading.value = false;
  }
};

const resetCacheForm = () => {
  cacheForm.tmdb_id = undefined;
  cacheForm.media_type = 'unknown';
  cacheForm.chinese_title = '';
  cacheForm.original_title = '';
  cacheForm.alias = '';
  cacheForm.source = 'manual';
  cacheForm.status = 'success';
  cacheForm.note = '';
};

const openCreateModal = () => {
  cacheEditId.value = null;
  resetCacheForm();
  cacheModalVisible.value = true;
};

const openEditModal = (record: CacheItem) => {
  cacheEditId.value = record.id;
  cacheForm.tmdb_id = record.tmdb_id;
  cacheForm.media_type = record.media_type || 'unknown';
  cacheForm.chinese_title = record.chinese_title || '';
  cacheForm.original_title = record.original_title || '';
  cacheForm.alias = record.alias || '';
  cacheForm.source = record.source || 'manual';
  cacheForm.status = record.status || 'success';
  cacheForm.note = record.note || '';
  cacheModalVisible.value = true;
};

const saveCacheRecord = async () => {
  if (!cacheForm.tmdb_id) {
    message.warning('请填写 TMDB ID');
    return;
  }

  const payload = {
    tmdb_id: cacheForm.tmdb_id,
    media_type: cacheForm.media_type,
    chinese_title: cacheForm.chinese_title,
    original_title: cacheForm.original_title,
    alias: cacheForm.alias,
    source: cacheForm.source,
    status: cacheForm.status,
    note: cacheForm.note
  };

  try {
    cacheSaving.value = true;
    if (cacheEditId.value) {
      await axios.put(`/api/sensitive/tmdb-alias-cache/${cacheEditId.value}`, payload);
    } else {
      await axios.post('/api/sensitive/tmdb-alias-cache', payload);
    }
    message.success('保存成功');
    cacheModalVisible.value = false;
    await fetchTmdbCache(cachePage.value);
  } catch (e: any) {
    message.error(e.response?.data?.detail || '保存失败');
  } finally {
    cacheSaving.value = false;
  }
};

const deleteCacheRecord = async (record: CacheItem) => {
  try {
    await axios.delete(`/api/sensitive/tmdb-alias-cache/${record.id}`);
    message.success('删除成功');
    selectedCacheRowKeys.value = selectedCacheRowKeys.value.filter((id) => id !== record.id);
    const currentPageCount = cacheItems.value.length;
    const targetPage = currentPageCount === 1 && cachePage.value > 1 ? cachePage.value - 1 : cachePage.value;
    await fetchTmdbCache(targetPage);
  } catch (e: any) {
    message.error(e.response?.data?.detail || '删除失败');
  }
};

const batchDeleteCacheRecords = async () => {
  if (selectedCacheRowKeys.value.length === 0) {
    message.warning('请先勾选要删除的记录');
    return;
  }

  try {
    const res = await axios.post('/api/sensitive/tmdb-alias-cache/batch-delete', {
      ids: selectedCacheRowKeys.value
    });
    if (res.data?.state) {
      message.success(`批量删除成功（${res.data.deleted || 0} 条）`);
      selectedCacheRowKeys.value = [];
      const shouldPrevPage = cacheItems.value.length === (res.data.deleted || 0) && cachePage.value > 1;
      await fetchTmdbCache(shouldPrevPage ? cachePage.value - 1 : cachePage.value);
    } else {
      message.error(res.data?.message || '批量删除失败');
    }
  } catch (e: any) {
    message.error(e.response?.data?.detail || '批量删除失败');
  }
};

const onCachePageChange = async (page: number) => {
  cachePage.value = page;
  await fetchTmdbCache(page);
};

const onCachePageSizeChange = async (_current: number, size: number) => {
  cachePageSize.value = size;
  cachePage.value = 1;
  await fetchTmdbCache(1);
};

onMounted(async () => {
  await loadReplaceConfig();
  await fetchTmdbCache();
});
</script>

<style scoped>
.sensitive-container {
  height: 100%;
  overflow-y: auto;
  padding-right: 8px;
}
</style>
