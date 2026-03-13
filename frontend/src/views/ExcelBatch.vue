<template>
  <div class="excel-batch-container">
    <a-alert
      v-if="isP115Restricted"
      message="当前 115 账号处于受限状态，批量转存已自动暂停，详情请查看机器人通知。"
      type="warning"
      show-icon
      banner
      style="margin-bottom: 16px; border-radius: 8px;"
    />
    <template v-if="!currentTaskId && tasks.length === 0 && !uploading">
      <!-- Initial Upload View -->
      <div class="empty-upload-view">
        <div class="header">
          <div class="header-left">
            <a-avatar :size="48" shape="square" style="background-color: #27ae60">
              <template #icon><FileExcelOutlined /></template>
            </a-avatar>
            <div class="header-text">
              <h1>文件批量转存分享</h1>
              <p>上传包含分享链接的 Excel 或 JSON 文件，批量转存并同步分享链接</p>
            </div>
          </div>
          <div class="header-right">
            <a-space>
              <a-button @click="downloadExcelTemplate">
                <template #icon><DownloadOutlined /></template>
                下载 Excel 模板
              </a-button>
              <a-button @click="downloadJsonTemplate">
                <template #icon><DownloadOutlined /></template>
                下载 JSON 模板
              </a-button>
            </a-space>
          </div>
        </div>

        <div class="main-empty-content">
          <div class="drop-zone">
             <a-upload-dragger
                name="file"
                :show-upload-list="false"
                :before-upload="handleBeforeUpload"
                accept=".xlsx,.xls,.csv,.json"
              >
                <p class="ant-upload-drag-icon">
                  <FileExcelOutlined style="color: #27ae60; font-size: 64px" />
                </p>
                <h2 class="ant-upload-text">请选择文件上传</h2>
                <p class="ant-upload-hint">支持 Excel (.xlsx, .xls, .csv) 和 Telegram 导出 (.json) 文件</p>
                <a-button type="primary" size="large" style="margin-top: 24px">
                  <template #icon><UploadOutlined /></template>
                  上传文件
                </a-button>
              </a-upload-dragger>
          </div>
        </div>
      </div>
    </template>

    <template v-else>
      <!-- Main Work View -->
      <div class="work-view">
        <!-- Sidebar Task List -->
        <div class="task-sidebar" :class="{ 'mobile-hidden': isMobile && currentTaskId }">
          <div class="sidebar-header">
            <span>任务列表</span>
            <div class="task-count-badge">{{ tasks.length }}</div>
          </div>
          <div class="sidebar-content">
            <template v-if="tasks.length === 0">
               <div class="empty-tasks">
                 <a-empty description="暂无任务" />
               </div>
            </template>
            <template v-else>
              <div 
                v-for="task in tasks" 
                :key="task.id" 
                class="task-item" 
                :class="{ active: currentTaskId === task.id }"
                @click="selectTask(task.id)"
              >
                <div class="task-icon-container">
                   <div class="excel-icon-box">
                      <FileExcelOutlined />
                   </div>
                </div>
                <div class="task-info">
                  <div class="task-name-row">
                    <div class="task-name">{{ task.name }}</div>
                  </div>
                  <div class="task-meta">
                    <a-tag :color="getStatusTagColor(task.status)" size="small">
                      {{ getStatusText(task.status) }}
                    </a-tag>
                    <span class="count">{{ task.total_count }} 条</span>
                  </div>
                </div>
                <div class="task-item-actions">
                   <a-button 
                     type="text" 
                     danger 
                     size="small"
                     @click.stop="handleDeleteTask(task.id)"
                     class="delete-item-btn"
                   >
                     <template #icon><DeleteOutlined /></template>
                   </a-button>
                </div>
              </div>
            </template>
          </div>
          <div class="sidebar-footer">
            <a-upload
              name="file"
              :show-upload-list="false"
              :before-upload="handleBeforeUpload"
              accept=".xlsx,.xls,.csv,.json"
            >
              <a-button type="primary" block>
                <template #icon><UploadOutlined /></template>
                上传文件
              </a-button>
            </a-upload>
          </div>
        </div>

        <!-- Task Content -->
        <div class="task-main" v-if="currentTaskId">
          <div class="task-header">
             <div class="header-left">
               <a-button v-if="isMobile" type="link" @click="currentTaskId = null">
                 <template #icon><LeftOutlined /></template>
               </a-button>
               <span class="task-title">{{ currentTask?.name }}</span>
             </div>
             <div class="header-right">
                <a-button ghost danger @click="handleDeleteTask()" size="small">删除任务</a-button>
             </div>
          </div>

          <!-- Stats Cards -->
          <div class="stats-cards">
            <a-row :gutter="16" type="flex">
              <a-col :flex="1">
                <div class="stat-card total">
                  <div class="stat-icon"><FileOutlined /></div>
                  <div class="stat-value">
                     <h3>{{ currentTask?.total_count || 0 }}</h3>
                     <p>总链接数</p>
                  </div>
                </div>
              </a-col>
              <a-col :flex="1">
                <div class="stat-card processing">
                  <div class="stat-icon"><SyncOutlined spin v-if="currentTask?.status === 'running'" /> <SyncOutlined v-else /></div>
                  <div class="stat-value">
                     <h3>{{ (currentTask?.success_count || 0) + (currentTask?.fail_count || 0) + (currentTask?.skipped_count || 0) }}</h3>
                     <p>已处理</p>
                     <div v-if="(currentTask?.status === 'running' || currentTask?.status === 'pausing' || currentTask?.status === 'cancelling') && (currentTask?.current_row > 0)" class="current-row-info">
                        {{ currentTask?.is_waiting ? '等待处理' : '正在处理' }}第 {{ currentTask?.current_row }} 行
                     </div>
                  </div>
                </div>
              </a-col>
              <a-col :flex="1">
                <div class="stat-card warning">
                  <div class="stat-icon"><StopOutlined /></div>
                  <div class="stat-value">
                     <h3>{{ currentTask?.skipped_count || 0 }}</h3>
                     <p>已跳过</p>
                  </div>
                </div>
              </a-col>
              <a-col :flex="1">
                <div class="stat-card success">
                  <div class="stat-icon"><CheckCircleOutlined /></div>
                  <div class="stat-value">
                     <h3>{{ currentTask?.success_count || 0 }}</h3>
                     <p>成功</p>
                  </div>
                </div>
              </a-col>
              <a-col :flex="1">
                <div class="stat-card error">
                  <div class="stat-icon"><CloseCircleOutlined /></div>
                  <div class="stat-value">
                     <h3>{{ currentTask?.fail_count || 0 }}</h3>
                     <p>失败</p>
                  </div>
                </div>
              </a-col>
            </a-row>
          </div>

          <!-- Control Section -->
          <div class="control-panel">
            <div class="section-title"><SettingOutlined /> 转存分享配置</div>
            <div class="control-form">
                <div class="form-row">
                  <div class="form-item">
                     <label>保存位置 (全局配置)</label>
                     <div class="fake-input disabled">
                        <span>{{ systemSaveDir }}</span>
                        <a-tooltip title="Excel 转存统一使用系统全局保存路径，如需更改请前往系统设置"><InfoCircleOutlined /></a-tooltip>
                     </div>
                  </div>
               </div>
                <div class="form-row">
                  <div class="form-item">
                     <label>转存间隔 (秒)</label>
                     <div class="interval-input">
                        <a-input-number v-model:value="intervalMin" :min="1" :precision="0" style="width: 80px" :disabled="['running', 'pausing', 'cancelling'].includes(currentTask?.status)" />
                        <span style="margin: 0 8px">-</span>
                        <a-input-number v-model:value="intervalMax" :min="1" :precision="0" style="width: 80px" :disabled="['running', 'pausing', 'cancelling'].includes(currentTask?.status)" />
                        <span style="margin-left: 8px">随机休眠</span>
                     </div>
                  </div>
                  <div class="form-item" style="margin-left: 24px">
                     <label>跳过前几条</label>
                     <a-input-number
                        v-model:value="skipCount"
                        :min="0"
                        :precision="0"
                         style="width: 100px"
                         :disabled="['running', 'pausing', 'cancelling'].includes(currentTask?.status)"
                      />
                  </div>
                  <div class="form-item" style="margin-left: 24px">
                     <label>第几行停止</label>
                     <a-tooltip :title="(stopRow ?? 0) > 0 && (stopRow ?? 0) <= (skipCount ?? 0) ? '停止行必须大于跳过条数' : '0 表示处理到最后一行（为空时提交自动重置为 0）'">
                       <a-input-number
                          v-model:value="stopRow"
                          :min="0"
                          :precision="0"
                          style="width: 100px"
                          :status="(stopRow ?? 0) > 0 && (stopRow ?? 0) <= (skipCount ?? 0) ? 'error' : ''"
                          :disabled="['running', 'pausing', 'cancelling'].includes(currentTask?.status)"
                       />
                     </a-tooltip>
                  </div>
                  <!-- <div class="form-item" style="margin-left: 24px; flex: none; width: 140px">
                     <label>跳过大包 (500文件限制)</label>
                     <div style="height: 32px; display: flex; align-items: center">
                        <a-switch v-model:checked="skipLargePackage" :disabled="['running', 'pausing', 'cancelling'].includes(currentTask?.status)" />
                     </div>
                  </div> -->
                </div>
                
                <div class="form-row">
                  <div class="form-item" style="flex: 1">
                    <label>白名单关键词 (包含任一则执行)</label>
                    <a-input v-model:value="whiteListKeywords" placeholder="关键词1, 关键词2 (逗号分隔)" :disabled="['running', 'pausing', 'cancelling'].includes(currentTask?.status)" />
                  </div>
                  <div class="form-item" style="flex: 1; margin-left: 24px">
                    <label>黑名单关键词 (包含任一则跳过)</label>
                    <a-input v-model:value="blackListKeywords" placeholder="关键词1, 关键词2 (逗号分隔)" :disabled="['running', 'pausing', 'cancelling'].includes(currentTask?.status)" />
                  </div>
                </div>

                <div class="form-row" v-if="tgChannels.length > 0">
                   <div class="form-item" style="width: 100%">
                      <label>推送频道</label>
                      <a-select 
                         v-model:value="selectedChannels" 
                         mode="multiple" 
                         style="width: 100%" 
                         placeholder="选择推送频道 (留空则不推送)"
                         :disabled="['running', 'pausing', 'cancelling'].includes(currentTask?.status)"
                         allow-clear
                         option-label-prop="label"
                      >
                         <a-select-option v-for="c in tgChannels" :key="c.id" :value="c.id" :label="c.name || c.id">
                           {{ c.name ? `${c.name} (${c.id})` : c.id }}
                         </a-select-option>
                      </a-select>
                   </div>
                </div>

               <div class="form-actions">
                  <a-space>
                    <a-button 
                      v-if="currentTask?.status === 'wait' || currentTask?.status === 'paused' || currentTask?.status === 'cancelled' || currentTask?.status === 'completed' || currentTask?.status === 'queued'" 
                      type="primary" 
                      @click="handleStartTask" 
                    >
                      {{ currentTask?.status === 'paused' ? '继续转存分享' : '开始转存分享' }}
                    </a-button>
                     <a-button v-if="currentTask?.status === 'running' || currentTask?.status === 'pausing'" type="primary" @click="handlePauseTask" :loading="pausing || currentTask?.status === 'pausing'" :disabled="currentTask?.status === 'pausing'">
                       暂停转存分享
                     </a-button>
                     <!-- User mentioned "暂停/继续、取消" -->
                     <a-button v-if="['running', 'paused', 'pausing'].includes(currentTask?.status)" @click="handleCancelTask" :loading="cancelling || currentTask?.status === 'cancelling'" :disabled="currentTask?.status === 'cancelling'">
                       取消转存分享
                     </a-button>
                  </a-space>
               </div>
            </div>
          </div>

            <!-- Links List -->
            <div class="links-list-container">
               <div class="section-title" style="margin-top: 16px">
                  <UnorderedListOutlined /> 分享链接列表
               </div>
            
            <!-- Filter & Search -->
            <div class="filter-bar">
               <a-select v-model:value="filterStatus" placeholder="选择状态" style="width: 120px" allow-clear @change="fetchTaskItems(1)">
                  <a-select-option value="待处理">待处理</a-select-option>
                  <a-select-option value="处理中">处理中</a-select-option>
                  <a-select-option value="成功">成功</a-select-option>
                  <a-select-option value="失败">失败</a-select-option>
                  <a-select-option value="跳过">跳过</a-select-option>
               </a-select>
               <a-input-search v-model:value="searchQuery" placeholder="搜索资源名称" style="width: 200px" @search="fetchTaskItems(1)" />
            </div>

            <a-table
              :columns="columns"
              :data-source="items"
              :pagination="false"
              :loading="itemsLoading"
              row-key="id"
              size="small"
              :scroll="{ y: '100%' }"
              @change="handleTableChange"
              class="flex-table"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'original_url'">
                  <a :href="record.original_url" target="_blank">{{ record.original_url }}</a>
                </template>
                <template v-if="column.key === 'status'">
                  <a-tag :color="getStatusColor(record.status)">{{ record.status }}</a-tag>
                </template>
                <template v-if="column.key === 'error_msg'">
                  <span v-if="record.status !== '成功'" style="color: #ff4d4f">{{ record.error_msg }}</span>
                </template>
                </template>
            </a-table>

            <div class="table-footer">
               <div class="footer-left">
               </div>
               <div class="footer-right">
                  <a-pagination
                    v-model:current="pagination.current"
                    v-model:pageSize="pagination.pageSize"
                    :total="pagination.total"
                    :show-size-changer="true"
                    :page-size-options="['10', '20', '50', '100', '200', '500', '1000', '2000', '5000', '10000']"
                    size="small"
                    @change="handleTableChange"
                  />
               </div>
            </div>
          </div>
        </div>
        
        <div class="task-main empty" v-else>
           <a-empty description="请选择一个任务或上传新文件" />
        </div>
      </div>
    </template>

    <!-- Column Mapping Modal -->
    <a-modal
      v-model:open="mappingModalVisible"
      title="文件列映射"
      @ok="handleMappingOk"
      :confirmLoading="creatingTask"
      width="800px"
    >
      <div class="mapping-content">
        <div class="file-summary">
           <div class="summary-item">
              <span class="label">文件名</span>
              <span class="value">{{ pendingFile?.name }}</span>
           </div>
           <div class="summary-item">
              <span class="label">数据行数</span>
              <span class="value">{{ parseResult?.total_rows }} 行</span>
           </div>
        </div>

        <div class="mapping-form">
           <div class="mapping-row">
              <div class="mapping-label"><LinkOutlined /> 链接列 <span>*</span></div>
              <a-select v-model:value="mapping.link" placeholder="请选择链接所在列" style="width: 100%">
                 <a-select-option v-for="h in parseResult?.headers" :key="h" :value="h">{{ h }}</a-select-option>
              </a-select>
              <div class="header-suggest" v-if="mapping.link">
                 样本：{{ parseResult?.preview[0][mapping.link] }}
              </div>
           </div>

           <div class="mapping-row">
              <div class="mapping-label"><FileTextOutlined /> 描述列 (可选)</div>
              <a-select v-model:value="mapping.title" placeholder="选择资源名称/标题列" style="width: 100%" allow-clear>
                 <a-select-option v-for="h in parseResult?.headers" :key="h" :value="h">{{ h }}</a-select-option>
              </a-select>
           </div>

           <div class="mapping-row">
              <div class="mapping-label"><KeyOutlined /> 访问码列 (可选)</div>
              <a-select v-model:value="mapping.code" placeholder="选择提取码/访问码列" style="width: 100%" allow-clear>
                 <a-select-option v-for="h in parseResult?.headers" :key="h" :value="h">{{ h }}</a-select-option>
              </a-select>
           </div>
        </div>

        <div class="mapping-preview">
           <h3>所有列预览</h3>
           <div class="preview-scroll">
             <table class="preview-table">
               <thead>
                 <tr>
                   <th>列</th>
                   <th>表头</th>
                   <th>样本值</th>
                   <th>建议</th>
                 </tr>
               </thead>
               <tbody>
                 <tr v-for="(h, idx) in parseResult?.headers" :key="h">
                   <td>{{ String.fromCharCode(65 + Number(idx)) }}</td>
                   <td>{{ h }}</td>
                   <td>{{ parseResult?.preview[0][h] }}</td>
                   <td>
                     <a-tag v-if="isLinkCol(h)" color="success">链接</a-tag>
                     <a-tag v-else-if="isTitleCol(h)" color="processing">描述</a-tag>
                     <a-tag v-else-if="isCodeCol(h)" color="warning">访问码</a-tag>
                   </td>
                 </tr>
               </tbody>
             </table>
           </div>
        </div>
      </div>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, computed, onUnmounted, watch } from 'vue';
import { 
  FileExcelOutlined, 
  DownloadOutlined, 
  UploadOutlined,
  FileOutlined,
  SyncOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  StopOutlined,
  SettingOutlined,
  UnorderedListOutlined,
  LinkOutlined,
  FileTextOutlined,
  KeyOutlined,
  LeftOutlined,
  DeleteOutlined,
  InfoCircleOutlined
} from '@ant-design/icons-vue';
import { message, Modal, Grid, theme } from 'ant-design-vue';
import axios from 'axios';

const { useToken } = theme;
const { token: antdToken } = useToken();

const useBreakpoint = Grid.useBreakpoint;
const screens = useBreakpoint();
const isMobile = computed(() => !screens.value.md && (screens.value.sm || screens.value.xs));

// State
const isP115Restricted = ref(false);
const tasks = ref<any[]>([]);
const currentTaskId = ref<number | null>(null);
const currentTask = computed(() => tasks.value.find(t => t.id === currentTaskId.value));
const items = ref<any[]>([]);
const itemsLoading = ref(false);
const pagination = reactive({
  current: 1,
  pageSize: 50,
  total: 0,
  showSizeChanger: true,
  pageSizeOptions: ['10', '20', '50', '100', '500', '1000', '2000', '5000', '10000'],
});

const filterStatus = ref<string | undefined>(undefined);
const searchQuery = ref('');
const intervalMin = ref(5);
const intervalMax = ref(10);
const systemSaveDir = ref('/分享保存');
const tgChannels = ref<any[]>([]);
const selectedChannels = ref<string[]>([]);
const whiteListKeywords = ref('');
const blackListKeywords = ref('');

// Upload & Mapping
const mappingModalVisible = ref(false);
const uploading = ref(false);
const creatingTask = ref(false);
const pausing = ref(false);
const cancelling = ref(false);
const skipCount = ref(0);
const stopRow = ref(0);
const skipLargePackage = ref(false);
const pendingFile = ref<File | null>(null);
const parseResult = ref<any>(null);
const mapping = reactive({
  link: undefined,
  title: undefined,
  code: undefined
});

// Timer for polling
let pollTimer: any = null;

const columns = [
  { title: '行号', dataIndex: 'row_index', key: 'row_index', width: 70 },
  { title: '分享链接', dataIndex: 'original_url', key: 'original_url', ellipsis: true },
  { title: '资源名称', dataIndex: 'title', key: 'title', ellipsis: true },
  { title: '消息时间', dataIndex: 'message_time', key: 'message_time', width: 120 },
  { title: '状态', dataIndex: 'status', key: 'status', width: 100 },
  { title: '新分享链接', dataIndex: 'new_share_url', key: 'new_share_url', ellipsis: true },
  { title: '错误信息', dataIndex: 'error_msg', key: 'error_msg', ellipsis: true },
];

// rowSelection logic removed

// Methods
const fetchTasks = async () => {
  try {
    const res = await axios.get('/api/excel/tasks');
    if (res.data.status === 'success') {
      tasks.value = res.data.data;
    }
  } catch (e) {
    console.error(e);
  }
};

const fetchSettings = async () => {
  try {
    const res = await axios.get('/api/config/');
    if (res.data.p115_save_dir) {
      systemSaveDir.value = res.data.p115_save_dir;
    }
    
    isP115Restricted.value = !!res.data.p115_restricted;

    if (res.data.tg_channels) {
      try {
        const channels = JSON.parse(res.data.tg_channels);
        tgChannels.value = channels.filter((c: any) => c.enabled);
        
        // Also add legacy channel ID if enabled and not in list
        const legacyId = res.data.tg_channel_id;
        if (legacyId && !tgChannels.value.find((c: any) => c.id === legacyId)) {
           tgChannels.value.unshift({ id: legacyId, enabled: true, name: '默认频道', concise: false });
        }
        
        // Default select all
        selectedChannels.value = tgChannels.value.map((c: any) => c.id);
      } catch (e) {
        console.error("Failed to parse tg_channels", e);
        tgChannels.value = [];
      }
    } else if (res.data.tg_channel_id) {
       // Only legacy
       tgChannels.value = [{ id: res.data.tg_channel_id, enabled: true, name: '默认频道', concise: false }];
       selectedChannels.value = [res.data.tg_channel_id];
    }
  } catch (e) {
    console.error('获取设置失败', e);
  }
};

const fetchCurrentTask = async () => {
  if (!currentTaskId.value) return;
  try {
    const res = await axios.get(`/api/excel/tasks/${currentTaskId.value}`);
    if (res.data.status === 'success') {
      const updatedTask = res.data.data;
      const index = tasks.value.findIndex(t => t.id === updatedTask.id);
      if (index !== -1) {
        tasks.value[index] = updatedTask;
      }
      
      // Update local state if task has saved channels
      if (updatedTask.target_channels) {
         // If task has specific channels saved, use them
         // Note: The backend might return them as a list directly
         selectedChannels.value = updatedTask.target_channels;
      }
      if (updatedTask.white_list_keywords !== undefined) whiteListKeywords.value = updatedTask.white_list_keywords || '';
      if (updatedTask.black_list_keywords !== undefined) blackListKeywords.value = updatedTask.black_list_keywords || '';
      if (updatedTask.skip_large_package !== undefined) skipLargePackage.value = updatedTask.skip_large_package || false;
    }
  } catch (e) {
    console.error('获取任务详情失败', e);
  }
};

const fetchTaskItems = async (page = 1, silent = false) => {
  if (!currentTaskId.value) return;
  if (!silent) itemsLoading.value = true;
  try {
    const res = await axios.get(`/api/excel/tasks/${currentTaskId.value}/items`, {
      params: {
        page,
        page_size: pagination.pageSize,
        status: filterStatus.value
      }
    });
    if (res.data.status === 'success') {
      items.value = res.data.data;
      pagination.total = res.data.total;
      pagination.current = page;
    }
  } catch (e) {
    console.error(e);
  } finally {
    itemsLoading.value = false;
  }
};

const selectTask = (id: number) => {
  currentTaskId.value = id;
  const task = tasks.value.find(t => t.id === id);
  skipCount.value = task?.skip_count || 0;
  stopRow.value = task?.stop_row || 0;
  intervalMin.value = task?.interval_min || 5;
  intervalMax.value = task?.interval_max || 10;
  
  if (task?.target_channels) {
      selectedChannels.value = task.target_channels;
  } else if (tasks.value.length > 0 && selectedChannels.value.length === 0) {
      // If no saved channels, default to all available (initially loaded)
      selectedChannels.value = tgChannels.value.map(c => c.id);
  }

  whiteListKeywords.value = task?.white_list_keywords || '';
  blackListKeywords.value = task?.black_list_keywords || '';
  skipLargePackage.value = task?.skip_large_package || false;
  
  pagination.current = 1;
  pagination.pageSize = 50;
  fetchTaskItems();
};

const handleBeforeUpload = async (file: File) => {
  pendingFile.value = file;
  const formData = new FormData();
  formData.append('file', file);
  
  uploading.value = true;
  try {
    const res = await axios.post('/api/excel/parse', formData);
    if (res.data.status === 'success') {
      parseResult.value = res.data.data;
      // Auto suggest mapping
      mapping.link = res.data.data.headers.find((h: string) => isLinkCol(h));
      mapping.title = res.data.data.headers.find((h: string) => isTitleCol(h));
      mapping.code = res.data.data.headers.find((h: string) => isCodeCol(h));
      
      mappingModalVisible.value = true;
    } else {
      message.error(res.data.message || '文件解析失败');
    }
  } catch (e) {
    message.error('上传解析失败');
  } finally {
    uploading.value = false;
  }
  return false; // Prevent auto upload
};

const handleMappingOk = async () => {
  if (!mapping.link) {
    message.warning('请选择链接列');
    return;
  }
  
  creatingTask.value = true;
  const formData = new FormData();
  formData.append('file', pendingFile.value!);
  formData.append('filename', pendingFile.value!.name);
  formData.append('mapping', JSON.stringify(mapping));
  
  try {
    const res = await axios.post('/api/excel/tasks', formData);
    if (res.data.status === 'success') {
      message.success('任务创建成功');
      mappingModalVisible.value = false;
      await fetchTasks();
      selectTask(res.data.task_id);
    } else {
      message.error(res.data.message || '创建任务失败');
    }
  } catch (e) {
    message.error('请求失败');
  } finally {
    creatingTask.value = false;
  }
};

const handleStartTask = async (forceParam?: any) => {
  if (!currentTaskId.value) return;
  // If paused, allow starting without selection (continue from where it left off)
  const isResume = currentTask.value?.status === 'paused';
  const force = forceParam === true;

  // 空值自动补默认值并提示
  const defaults: string[] = [];
  if (skipCount.value == null) { skipCount.value = 0; defaults.push('跳过前几条 → 0'); }
  if (stopRow.value == null) { stopRow.value = 0; defaults.push('第几行停止 → 0'); }
  if (intervalMin.value == null) { intervalMin.value = 5; defaults.push('转存间隔最小值 → 5'); }
  if (intervalMax.value == null) { intervalMax.value = 10; defaults.push('转存间隔最大值 → 10'); }
  if (defaults.length > 0) {
    message.warning(`空值已重置为默认值：${defaults.join('，')}`);
  }

  // 校验：stop_row 有值时必须大于 skip_count
  if (stopRow.value > 0 && stopRow.value <= skipCount.value) {
    message.error('停止行必须大于跳过条数');
    return;
  }

  try {
    await axios.post(`/api/excel/tasks/${currentTaskId.value}/start`, {
      skip_count: skipCount.value,
      stop_row: stopRow.value,
      interval_min: intervalMin.value,
      interval_max: intervalMax.value,
      target_channels: selectedChannels.value,
      white_list_keywords: whiteListKeywords.value,
      black_list_keywords: blackListKeywords.value,
      skip_large_package: skipLargePackage.value,
      force: force

    });
    message.success(isResume ? '正在继续转存分享...' : '正在启动转存分享...');
    await fetchTasks();
    await fetchSettings();
  } catch (e: any) {
    if (e.response && e.response.status === 400) {
      const errorMsg = e.response.data.detail || '启动失败';
      if (!force && errorMsg.includes('强制继续')) {
        Modal.confirm({
          title: '系统检测到受限状态',
          content: '继续启动将尝试发起探路请求（强制解除受限标记），如果 115 依然限制可能会增加风险或立刻重新冻结。您确定要强行继续吗？',
          okText: '强行继续',
          okType: 'danger',
          cancelText: '取消',
          onOk() {
            handleStartTask(true);
          }
        });
      } else {
        message.warning(errorMsg);
      }
    } else {
      message.error(isResume ? '继续失败' : '启动失败');
    }
  }
};

const handlePauseTask = async () => {
  if (!currentTaskId.value) return;
  pausing.value = true;
  try {
    await axios.post(`/api/excel/tasks/${currentTaskId.value}/pause`);
    message.success('已暂停');
    await fetchCurrentTask(); // Sync current task immediately
    await fetchTaskItems(pagination.current, true); // Sync table items immediately
    // Delay sidebar refresh by 3s as requested
    setTimeout(() => {
      fetchTasks();
    }, 3000);
  } catch (e) {
    message.error('请求失败');
  } finally {
    pausing.value = false;
  }
};

const handleCancelTask = async () => {
  if (!currentTaskId.value) return;
  Modal.confirm({
     title: '确认取消转存？',
     content: '取消后将停止当前任务的处理，是否继续？',
     onOk: async () => {
        cancelling.value = true;
        try {
          await axios.post(`/api/excel/tasks/${currentTaskId.value}/cancel`); 
          message.success('任务已取消处理');
          await fetchCurrentTask(); // Sync current task immediately
          await fetchTaskItems(pagination.current, true); // Sync table items immediately
          // Delay sidebar refresh by 3s
          setTimeout(() => {
            fetchTasks();
          }, 3000);
        } catch (e) {
          message.error('请求失败');
        } finally {
          cancelling.value = false;
        }
     }
  });
};

const handleDeleteTask = (taskId?: number) => {
  const targetId = taskId || currentTaskId.value;
  if (!targetId) return;
  
  Modal.confirm({
    title: '确认删除任务？',
    content: '此操作将彻底删除任务及其所有链接记录，是否继续？',
    okType: 'danger',
    onOk: async () => {
      try {
        await axios.delete(`/api/excel/tasks/${targetId}`);
        message.success('任务已删除');
        if (currentTaskId.value === targetId) {
          currentTaskId.value = null;
        }
        await fetchTasks();
      } catch (e) {
        message.error('删除失败');
      }
    }
  });
};

const getStatusText = (status: string) => {
  const map: any = {
    'wait': '待处理',
    'running': '运行中',
    'paused': '已暂停',
    'completed': '已完成',
    'cancelled': '已取消',
    'queued': '排队中',
    'pausing': '暂停中...',
    'cancelling': '取消中...'
  };
  return map[status] || status;
};

const getStatusTagColor = (status: string) => {
  const map: any = {
    'wait': 'default',
    'running': 'processing',
    'paused': 'warning',
    'completed': 'success',
    'cancelled': 'error',
    'queued': 'cyan',
    'pausing': 'orange',
    'cancelling': 'volcano'
  };
  return map[status] || 'default';
};

// 移除 skipCount 的 watch，因为用户要求修改该值时不触发表格请求

const handleTableChange = (page: number, pageSize: number) => {
  pagination.pageSize = pageSize;
  fetchTaskItems(page);
};

// selectAll removed

const downloadExcelTemplate = () => {
  window.open('/static/template/分享链接导入模板.xlsx', '_blank');
};

const downloadJsonTemplate = () => {
  window.open('/static/template/telegram_template.json', '_blank');
};

const getStatusColor = (status: string) => {
  switch (status) {
    case '成功': return 'success';
    case '失败': return 'error';
    case '处理中': return 'processing';
    case '跳过': return 'default';
    case '待处理': return 'warning';
    default: return 'default';
  }
};

// Helpers for suggest
const isLinkCol = (h: string) => /链接|url|link|s\/|115/i.test(h);
const isTitleCol = (h: string) => /标题|名称|资源|name|title/i.test(h);
const isCodeCol = (h: string) => /提取码|访问码|密码|code|pwd|password|msg/i.test(h);

watch([currentTaskId, () => currentTask.value?.status], ([newId, status]) => {
  if (pollTimer) {
     clearInterval(pollTimer);
     pollTimer = null;
  }
  if (newId && ['running', 'pausing', 'cancelling'].includes(status)) {
    pollTimer = setInterval(async () => {
      await fetchTasks(); // Sync sidebar periodically
      await fetchCurrentTask();
      
      // Auto-pagination logic
      if (currentTask.value?.current_row) {
        const targetPage = Math.ceil(currentTask.value.current_row / pagination.pageSize);
        if (targetPage !== pagination.current) {
          fetchTaskItems(targetPage, true);
        } else {
          fetchTaskItems(pagination.current, true);
        }
      } else {
        fetchTaskItems(pagination.current, true);
      }
    }, 3000);
  }
}, { immediate: true });

onMounted(() => {
  fetchTasks();
  fetchSettings();
});

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer);
});
</script>

<style scoped>
.excel-batch-container {
  height: 100%;
  color: v-bind('antdToken.colorText');
}

.empty-upload-view {
  max-width: 1200px;
  margin: 0 auto;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 48px;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.header-text h1 {
  margin: 0;
  font-size: 24px;
}

.header-text p {
  margin: 4px 0 0;
  color: v-bind('antdToken.colorTextDescription');
}

.main-empty-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 64px 0;
}

.drop-zone {
  width: 100%;
  max-width: 600px;
}

.work-view {
  display: flex;
  height: 100%;
  gap: 16px;
  min-height: 0;
}

.task-sidebar {
  width: 260px;
  border-right: 1px solid v-bind('antdToken.colorBorderSecondary');
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}

.sidebar-header {
  height: 64px;
  padding: 0 16px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid v-bind('antdToken.colorBorderSecondary');
  flex-shrink: 0;
}

.sidebar-header span {
  font-size: 16px;
  font-weight: bold;
}

.task-count-badge {
  background: #1890ff;
  color: white;
  min-width: 22px;
  height: 22px;
  padding: 0 6px;
  border-radius: 11px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 500;
}

.sidebar-content {
  flex: 1;
  overflow-y: auto;
  padding: 8px 0;
}

.task-item {
  display: flex;
  align-items: center;
  padding: 12px;
  margin: 12px;
  border-radius: 12px;
  cursor: pointer;
  transition: all 0.2s;
  border: 1px solid v-bind('antdToken.colorBorderSecondary');
  position: relative;
  background: v-bind('antdToken.colorBgContainer');
}

.task-item:hover {
  background: v-bind('antdToken.colorFillTertiary');
  border-color: v-bind('antdToken.colorPrimary');
}

.task-item.active {
  background: v-bind('antdToken.colorFillTertiary');
  border-color: v-bind('antdToken.colorPrimary');
  box-shadow: 0 0 0 1px v-bind('antdToken.colorPrimary');
}

.task-icon-container {
  margin-right: 12px;
}

.excel-icon-box {
  width: 48px;
  height: 48px;
  background: #228b22;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
  font-size: 24px;
}

.task-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.task-name-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.task-name {
  font-size: 15px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: v-bind('antdToken.colorText');
}

.task-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
  color: v-bind('antdToken.colorTextDescription');
}

.task-item-actions {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  opacity: 0;
  transition: opacity 0.2s;
}

.task-item:hover .task-item-actions {
  opacity: 1;
}

.delete-item-btn {
  background: v-bind('antdToken.colorBgBase') !important;
}

.status-running { color: v-bind('antdToken.colorPrimary'); }
.status-paused { color: v-bind('antdToken.colorWarning'); }
.status-completed { color: v-bind('antdToken.colorSuccess'); }
.status-cancelled { color: v-bind('antdToken.colorError'); }

.sidebar-footer {
  padding: 16px;
  border-top: 1px solid v-bind('antdToken.colorBorderSecondary');
}

.task-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding-right: 8px;
}

.task-main.empty {
  align-items: center;
  justify-content: center;
}

.task-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.task-title {
  font-size: 18px;
  font-weight: bold;
}

.stats-cards {
  margin-bottom: 24px;
}

.stat-card {
  padding: 16px;
  background: v-bind('antdToken.colorFillAlter');
  border-radius: 8px;
  display: flex;
  align-items: center;
  gap: 16px;
  height: 80px;
}

.stat-icon {
  font-size: 24px;
  padding: 8px;
  border-radius: 4px;
  background: v-bind('antdToken.colorBgContainer');
}

.total .stat-icon { color: v-bind('antdToken.colorPrimary'); }
.processing .stat-icon { color: v-bind('antdToken.colorTextPlaceholder'); }
.success .stat-icon { color: v-bind('antdToken.colorSuccess'); }
.error .stat-icon { color: v-bind('antdToken.colorError'); }

.stat-value h3 {
  margin: 0;
  font-size: 20px;
}

.stat-value p {
  margin: 0;
  font-size: 12px;
  color: v-bind('antdToken.colorTextDescription');
}

.control-panel {
  padding: 16px;
  background: v-bind('antdToken.colorFillAlter');
  border-radius: 8px;
  margin-bottom: 24px;
}

.section-title {
  font-weight: bold;
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.control-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.form-row {
  display: flex;
  gap: 24px;
}

.form-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
}

.form-item label {
  font-size: 12px;
  color: v-bind('antdToken.colorTextDescription');
}

.fake-input {
  height: 32px;
  background: v-bind('antdToken.colorFillTertiary');
  border-radius: 4px;
  display: flex;
  align-items: center;
  padding: 0 12px;
  justify-content: space-between;
  font-size: 14px;
}

.form-item.disabled { color: v-bind('antdToken.colorTextDisabled'); }

.form-actions {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 16px;
}

.form-actions .hint {
  font-size: 12px;
  color: v-bind('antdToken.colorError');
}

.links-list-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
}

.flex-table {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

:deep(.flex-table .ant-table-wrapper) {
  height: 100%;
  display: flex;
  flex-direction: column;
}

:deep(.flex-table .ant-spin-nested-loading) {
  flex: 1;
  min-height: 0;
}

:deep(.flex-table .ant-spin-container) {
  height: 100%;
  display: flex;
  flex-direction: column;
}

:deep(.flex-table .ant-table) {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

:deep(.flex-table .ant-table-container) {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

:deep(.flex-table .ant-table-body) {
  flex: 1 !important;
  overflow-y: auto !important;
}

:deep(.flex-table .ant-table-pagination) {
  margin: 16px 0 0 0 !important;
  flex-shrink: 0;
}

.list-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.filter-bar {
  display: flex;
  gap: 16px;
  margin-bottom: 16px;
}

.mapping-content {
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.file-summary {
  padding: 16px;
  background: v-bind('antdToken.colorFillTertiary');
  border-radius: 8px;
  display: flex;
  gap: 48px;
}

.summary-item {
  display: flex;
  flex-direction: column;
}

.summary-item .label {
  font-size: 12px;
  color: v-bind('antdToken.colorTextDescription');
}

.summary-item .value {
  font-weight: bold;
}

.mapping-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.mapping-label {
  margin-bottom: 8px;
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 8px;
}

.mapping-label span { color: v-bind('antdToken.colorError'); }

.header-suggest {
  font-size: 11px;
  color: v-bind('antdToken.colorTextDescription');
  margin-top: 4px;
}

.preview-scroll {
  max-height: 300px;
  overflow-y: auto;
  border: 1px solid v-bind('antdToken.colorBorderSecondary');
  border-radius: 4px;
}

.preview-table {
  width: 100%;
  border-collapse: collapse;
}

.preview-table th, .preview-table td {
  padding: 8px 12px;
  text-align: left;
  border-bottom: 1px solid v-bind('antdToken.colorBorderSecondary');
  font-size: 13px;
}

.preview-table th {
  background: v-bind('antdToken.colorFillAlter');
  position: sticky;
  top: 0;
}

/* Table Footer with Skip Input */
.table-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  background: v-bind('antdToken.colorFillAlter');
  border-top: 1px solid v-bind('antdToken.colorBorderSecondary');
  border-radius: 0 0 8px 8px;
  margin-top: -1px; /* Overlap with table border if any */
}

.footer-left {
  display: flex;
  align-items: center;
}

.skip-input {
  display: flex;
  align-items: center;
  gap: 8px;
  background: v-bind('antdToken.colorBgContainer');
  padding: 4px 12px;
  border-radius: 4px;
  border: 1px solid v-bind('antdToken.colorBorder');
}

.skip-input .label {
  font-size: 13px;
  color: v-bind('antdToken.colorTextSecondary');
}

@media (max-width: 768px) {
  .work-view { flex-direction: column; }
  .task-sidebar { width: 100%; height: 200px; border-right: none; border-bottom: 1px solid v-bind('antdToken.colorBorderSecondary'); }
  .mobile-hidden { display: none; }
  .form-row { flex-direction: column; gap: 12px; }
  .stat-card { flex-direction: column; height: auto; padding: 12px; text-align: center; }
  .stats-cards .ant-col { margin-bottom: 12px; }
}
.current-row-info {
  font-size: 12px;
  color: #1890ff;
  margin-top: 4px;
}
</style>
