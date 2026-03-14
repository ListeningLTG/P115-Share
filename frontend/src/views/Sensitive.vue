<template>
  <div class="sensitive-container">
    <a-typography-title :level="4" style="margin-bottom: 24px">敏感词库管理</a-typography-title>

    <a-card title="TMDB 配置" style="margin-bottom: 24px">
      <a-form :model="configForm" layout="vertical">
        <a-form-item label="TMDB API Key">
          <a-input-password
            v-model:value="configForm.api_key"
            placeholder="请输入 TMDB API Key"
          />
        </a-form-item>

        <a-form-item label="关键词列表">
          <template #extra>
            <div style="font-size: 12px; color: #999; margin-top: 4px">
              多个关键词用英文逗号分隔。系统会自动将关键词名称解析为 TMDB ID，然后精准拉取匹配电影。
            </div>
          </template>
          <a-textarea
            v-model:value="configForm.keywords"
            placeholder="erotic movie,eroticism,softcore,sexual fantasy,unusual sexual practices,lesbian sex,gay sex,erotic thriller"
            :rows="4"
          />
        </a-form-item>

        <a-space>
          <a-button type="primary" @click="saveConfig" :loading="savingConfig">
            保存配置
          </a-button>
          <a-button @click="testConnection" :loading="testingConnection">
            测试连接
          </a-button>
        </a-space>
      </a-form>
    </a-card>

    <a-card title="爬取控制" style="margin-bottom: 24px">
      <a-space direction="vertical" style="width: 100%">
        <a-alert
          type="info"
          show-icon
          style="margin-bottom: 8px"
          message="关键词精准模式：将先解析关键词 ID，再直接拉取匹配电影，并自动获取美国分级信息"
        />

        <div style="font-size: 13px; color: #666; margin-bottom: 4px">
          上次全量同步时间：
          <b>{{ lastSyncAt || '从未同步' }}</b>
        </div>

        <a-space>
          <a-button
            type="primary"
            @click="startFetch"
            :loading="fetchStatus.status === 'running'"
            :disabled="fetchStatus.status === 'running'"
          >
            全量爬取
          </a-button>
          <a-tooltip title="按上映日期倒序拉取，遇到已有记录即停止，只新增未入库的电影">
            <a-button
              @click="startIncrementalSync"
              :loading="fetchStatus.status === 'running'"
              :disabled="fetchStatus.status === 'running'"
            >
              增量同步
            </a-button>
          </a-tooltip>
          <a-button
            danger
            @click="stopFetch"
            :disabled="fetchStatus.status !== 'running'"
          >
            停止
          </a-button>
          <a-button @click="refreshStatus">刷新状态</a-button>
        </a-space>

        <div v-if="fetchStatus.status !== 'idle'">
          <a-progress
            :percent="fetchProgress"
            :status="fetchStatus.status === 'error' ? 'exception' : fetchStatus.status === 'completed' || fetchStatus.status === 'stopped' ? 'success' : 'active'"
          />
          <div style="margin-top: 8px; color: #666; font-size: 13px">
            状态: <b>{{ fetchStatusText }}</b> | 已保存: {{ fetchStatus.current }}
            <span v-if="fetchStatus.total > 0"> / 共 {{ fetchStatus.total }}</span>
            <br />
            {{ fetchStatus.message }}
          </div>
        </div>
      </a-space>
    </a-card>

    <a-card title="电影列表">
      <template #extra>
        <a-space>
          <a-input-search
            v-model:value="searchText"
            placeholder="搜索名称或 TMDB ID"
            style="width: 220px"
            @search="handleSearch"
          />
          <a-popconfirm
            title="确定要清空所有数据吗？"
            ok-text="确定"
            cancel-text="取消"
            @confirm="clearAllMovies"
          >
            <a-button danger>清空所有数据</a-button>
          </a-popconfirm>
        </a-space>
      </template>

      <a-table
        :columns="columns"
        :data-source="movies"
        :loading="loadingMovies"
        :pagination="pagination"
        @change="handleTableChange"
        row-key="id"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'tmdb_id'">
            <a
              :href="`https://www.themoviedb.org/movie/${record.tmdb_id}`"
              target="_blank"
              rel="noopener noreferrer"
            >{{ record.tmdb_id }}</a>
          </template>
          <template v-else-if="column.key === 'keywords'">
            <a-tag v-for="(keyword, idx) in record.keywords.slice(0, 3)" :key="idx" color="blue" style="margin: 2px">
              {{ keyword }}
            </a-tag>
            <span v-if="record.keywords.length > 3">+{{ record.keywords.length - 3 }}</span>
          </template>
          <template v-else-if="column.key === 'action'">
            <a-popconfirm
              title="确定删除这部电影吗？"
              ok-text="确定"
              cancel-text="取消"
              @confirm="deleteMovie(record.id)"
            >
              <a-button type="link" danger size="small">删除</a-button>
            </a-popconfirm>
          </template>
        </template>
      </a-table>
    </a-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, computed, onUnmounted } from 'vue';
import axios from 'axios';
import { message } from 'ant-design-vue';

const configForm = reactive({
  api_key: '',
  country: 'US',
  certifications: ['R', 'NC-17'],
  keywords: '',
  use_keyword_filter: true
});

const savingConfig = ref(false);
const testingConnection = ref(false);
const loadingMovies = ref(false);
const searchText = ref('');
const lastSyncAt = ref<string>('');
const movies = ref<any[]>([]);
const sortField = ref('created_at');
const sortOrder = ref('desc');
const pagination = reactive({
  current: 1,
  pageSize: 20,
  total: 0
});

const fetchStatus = ref({
  status: 'idle',
  current: 0,
  total: 0,
  message: ''
});

let statusInterval: any = null;

const fetchProgress = computed(() => {
  if (fetchStatus.value.total === 0) return 0;
  return Math.round((fetchStatus.value.current / fetchStatus.value.total) * 100);
});

const fetchStatusText = computed(() => {
  const statusMap: Record<string, string> = {
    idle: '空闲',
    running: '运行中',
    completed: '已完成',
    stopped: '已停止',
    error: '错误'
  };
  return statusMap[fetchStatus.value.status] || '未知';
});

const columns = [
  {
    title: 'TMDB ID',
    dataIndex: 'tmdb_id',
    key: 'tmdb_id',
    width: 100,
    sorter: true
  },
  {
    title: '电影名称',
    dataIndex: 'title',
    key: 'title',
    width: 160,
    sorter: true
  },
  {
    title: '中文译名',
    dataIndex: 'chinese_title',
    key: 'chinese_title',
    width: 160,
    sorter: true
  },
  {
    title: '原始名称',
    dataIndex: 'original_title',
    key: 'original_title',
    width: 160,
    sorter: true
  },
  {
    title: '关键词',
    dataIndex: 'keywords',
    key: 'keywords',
    width: 200
  },
  {
    title: '分级',
    dataIndex: 'certification',
    key: 'certification',
    width: 80
  },
  {
    title: '上映日期',
    dataIndex: 'release_date',
    key: 'release_date',
    width: 120,
    sorter: true
  },
  { title: '操作', key: 'action', width: 80 }
];

const loadConfig = async () => {
  try {
    const res = await axios.get('/api/sensitive/config');
    if (res.data.status === 'success' && res.data.data) {
      configForm.api_key = res.data.data.api_key || '';
      configForm.country = res.data.data.country || 'US';
      configForm.certifications = res.data.data.certifications || ['R', 'NC-17'];
      configForm.keywords = res.data.data.keywords || '';
      configForm.use_keyword_filter = true; // always keyword mode
      lastSyncAt.value = res.data.data.last_sync_at || '';
    }
  } catch (e) {
    console.error('加载配置失败:', e);
  }
};

const saveConfig = async () => {
  if (!configForm.api_key) {
    message.warning('请输入 TMDB API Key');
    return;
  }

  try {
    savingConfig.value = true;
    // Force keyword mode enabled
    const payload = { ...configForm, use_keyword_filter: true };
    const res = await axios.post('/api/sensitive/config', payload);
    if (res.data.status === 'success') {
      message.success('配置已保存');
    } else {
      message.error(res.data.message || '保存失败');
    }
  } catch (e: any) {
    message.error(e.response?.data?.detail || '保存失败');
  } finally {
    savingConfig.value = false;
  }
};

const testConnection = async () => {
  if (!configForm.api_key) {
    message.warning('请输入 TMDB API Key');
    return;
  }

  try {
    testingConnection.value = true;
    const res = await axios.post('/api/sensitive/test-connection', {
      api_key: configForm.api_key
    });
    if (res.data.status === 'success') {
      message.success(res.data.message);
    } else {
      message.error(res.data.message);
    }
  } catch (e: any) {
    message.error(e.response?.data?.detail || '测试失败');
  } finally {
    testingConnection.value = false;
  }
};

const startFetch = async () => {
  if (!configForm.api_key) {
    message.warning('请先配置并保存 TMDB API Key');
    return;
  }

  try {
    const res = await axios.post('/api/sensitive/fetch', {
      country: 'US',
      certifications: []
    });
    if (res.data.status === 'success') {
      message.success('爬取任务已启动');
      startStatusPolling();
    } else {
      message.error(res.data.message);
    }
  } catch (e: any) {
    message.error(e.response?.data?.detail || '启动失败');
  }
};

const startIncrementalSync = async () => {
  if (!configForm.api_key) {
    message.warning('请先配置并保存 TMDB API Key');
    return;
  }

  try {
    const res = await axios.post('/api/sensitive/incremental-sync');
    if (res.data.status === 'success') {
      message.success('增量同步已启动');
      startStatusPolling();
    } else {
      message.error(res.data.message);
    }
  } catch (e: any) {
    message.error(e.response?.data?.detail || '启动失败');
  }
};

const stopFetch = async () => {
  try {
    const res = await axios.post('/api/sensitive/stop');
    if (res.data.status === 'success') {
      message.success('正在停止爬取任务');
    }
  } catch (e: any) {
    message.error(e.response?.data?.detail || '停止失败');
  }
};

const refreshStatus = async () => {
  try {
    const res = await axios.get('/api/sensitive/status');
    if (res.data.status === 'success') {
      fetchStatus.value = res.data.data;

      if (fetchStatus.value.status === 'running') {
        startStatusPolling();
      } else {
        stopStatusPolling();
      }
    }
  } catch (e) {
    console.error('刷新状态失败:', e);
  }
};

const startStatusPolling = () => {
  if (statusInterval) return;
  statusInterval = setInterval(async () => {
    await refreshStatus();
    if (fetchStatus.value.status !== 'running') {
      stopStatusPolling();
      await loadMovies();
      await loadConfig(); // 刷新 last_sync_at
    }
  }, 2000);
};

const stopStatusPolling = () => {
  if (statusInterval) {
    clearInterval(statusInterval);
    statusInterval = null;
  }
};

const loadMovies = async () => {
  try {
    loadingMovies.value = true;
    const res = await axios.get('/api/sensitive/movies', {
      params: {
        page: pagination.current,
        page_size: pagination.pageSize,
        search: searchText.value,
        sort_field: sortField.value,
        sort_order: sortOrder.value
      }
    });
    if (res.data.status === 'success') {
      movies.value = res.data.data.items;
      pagination.total = res.data.data.total;
    }
  } catch (e) {
    console.error('加载电影列表失败:', e);
    message.error('加载失败');
  } finally {
    loadingMovies.value = false;
  }
};

const handleSearch = () => {
  pagination.current = 1;
  loadMovies();
};

const handleTableChange = (pag: any, _filters: any, sorter: any) => {
  pagination.current = pag.current;
  pagination.pageSize = pag.pageSize;

  if (sorter && sorter.field) {
    // Map column dataIndex to backend sort field
    const fieldMap: Record<string, string> = {
      tmdb_id: 'tmdb_id',
      title: 'title',
      chinese_title: 'chinese_title',
      original_title: 'original_title',
      release_date: 'release_date'
    };
    sortField.value = fieldMap[sorter.field] || 'created_at';
    sortOrder.value = sorter.order === 'ascend' ? 'asc' : 'desc';
  }

  loadMovies();
};

const deleteMovie = async (id: number) => {
  try {
    const res = await axios.delete(`/api/sensitive/movies/${id}`);
    if (res.data.status === 'success') {
      message.success('删除成功');
      await loadMovies();
    } else {
      message.error(res.data.message);
    }
  } catch (e: any) {
    message.error(e.response?.data?.detail || '删除失败');
  }
};

const clearAllMovies = async () => {
  try {
    const res = await axios.post('/api/sensitive/clear');
    if (res.data.status === 'success') {
      message.success('已清空所有数据');
      await loadMovies();
    } else {
      message.error(res.data.message);
    }
  } catch (e: any) {
    message.error(e.response?.data?.detail || '清空失败');
  }
};

onMounted(async () => {
  await loadConfig();
  await loadMovies();
  await refreshStatus();
});

onUnmounted(() => {
  stopStatusPolling();
});
</script>

<style scoped>
.sensitive-container {
  height: 100%;
  overflow-y: auto;
  padding-right: 8px;
}
</style>
