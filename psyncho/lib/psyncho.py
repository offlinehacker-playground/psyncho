import pod
import pod.list
import os
import re
import fs
import stat

from fs.opener import fsopendir
from fs.utils import copyfile
from copy import deepcopy
from datetime import timedelta
from datetime import datetime
from time import mktime

from extra import Enumerate, is_file, is_lnk, is_dir, get_fmod

PathStatus= Enumerate("undef include ignore stop")

class PathPart(pod.Object):
    def __init__(self, name, parent= None, pathStatus= PathStatus.undef, depth= 0):
        pod.Object.__init__(self)
        
        self.name= name # Value of the path
        self.PathStatus= pathStatus
        
        self.children= []
        self.parent= parent
        if parent!=None:
            self.parent.children.append(self)

        # We must store depth to help while getting config layer with deepest path.
        self.depth= depth

        self.pathConfig=[] # Config as key->value store.
                
    def CreatePath(self, path, pathStatus= PathStatus.undef):
        # If we are at the end of creation return self.
        if(path==[]):
            return self
        
        # Here the creation gets done
        pathPart= PathPart(path[0], self, pathStatus, self.depth+1)
        
        # Recursively call for full path creation
        return pathPart.CreatePath(path[1:])   
        
    def GetPathPart(self, path, new=False):
        if(path==[]):
            return self.parent
        if(self.name!=path[0]):
            return None
            
        for child in self.children:
            tpathPart= child.GetPathPart(path[1:], new)
            if(tpathPart!=None):
                return tpathPart
            
        if(new == True):
            return self.CreatePath(path[1:])

        return self

    def PathExists(self, path):
        if(path==[]):
            return self.parent
        if(self.name!=path[0]):
            return False
            
        for child in self.children:
            tpathPart= child.PathExists(path[1:])
            if(tpathPart):
                return True
            
        return False
        
    def GetLastPart(self, path, report_truncated= False):
        if(path==[]):
            if report_truncated:
                return (False, self.parent)
            else:
                return self.parent

        match1= re.match('^\{(?P<regex>\S+)\}$',self.name)
        match2= re.match('^\|(?P<regex>\S+)\|$',self.name)
        if match1:
            regex= match1.group('regex')
            if not re.match(regex, path[0]):
                if report_truncated:
                    return (False,None)
                else:
                    return None
        # Regex of type2 matches whole string path from here on.
        elif match2:
            regex= match2.group('regex')
            stringpath= '/'.join(path)
            splited= re.split(regex, stringpath,1)
            if splited[0]!=stringpath and len(stringpath)>len(splited[1]):
                path= splited[1].split("/")
            else:
                if report_truncated:
                    return (False,None)
                else:
                    return None
        elif(self.name!=path[0]):
            if report_truncated:
                return (False,None)
            else:
                return None
        
        for child in self.children:
            if report_truncated:
                (truncated, tpathPart)= child.GetLastPart(path[1:], report_truncated)
            else:
                tpathPart= child.GetLastPart(path[1:], report_truncated)
            if(tpathPart!=None):
                if report_truncated:
                    return (truncated,tpathPart)
                else:
                    return tpathPart 
        
        """That's the key and only point where we report truncated.
        That's in a case when searched path is longer than configured one."""
        if report_truncated and len(path[1:])>0:
            return (True, self)
         
        if report_truncated:
            return (False,self)
        else:
            return self
        
    def DelPathPart(self, path):
        tpathPart= self.GetPathPart(path, False)
        if(tpathPart==None):
            return
        
        tpathPart.parent.children.remove(tpathPart)
        tpathPart.delete()
        
    def SetPathConfig(self, path, key, value):
        # PathPart must already exist to set config.
        pathPart= self.GetPathPart(path, False)
        if pathPart:
            pathPart.pathConfig[key]= value

    def GetPathConfig(self, path, key):
        # PathPart must already exist to get config.
        pathPart= self.GetPathPart(path, False)
        if pathPart:
            return pathPart.pathConfig[key]

    def SetPathStatus(self, path, lPathStatus):
        pathPart= self.GetPathPart(path, True) #This function should never return None
        pathPart.PathStatus = lPathStatus
        
    def GetPathStatus(self, path):
        tpathPart= self.GetLastPart(path)
        
        if(tpathPart==None):
            return PathStatus.undef
        
        return tpathPart.PathStatus
    
    def __str__(self):
        absolute_path= '/'.join(self.AbsolutePath())
        if self.PathStatus==PathStatus.ignore:
            str_status= "ignore"
        elif self.PathStatus==PathStatus.include:
            str_status= "include"
        elif self.PathStatus==PathStatus.stop:
            str_status= "stop"
        elif self.PathStatus==PathStatus.undef:
            str_status= "undef"
        return absolute_path + " [" + str_status + "]"
    
    def AbsolutePath(self):
        path=[self.name]
        parent= self.parent
        while parent:
            path.append(parent.name)
            parent= parent.parent
        path.reverse()
        return path 
        
    def pre_delete(self):
        for child in self.children:
            self.children.remove(child)
            child.delete()
        
    def __deepcopy__(self, memo):
        not_there = []
        existing = memo.get(self, not_there)
        if existing is not not_there:
            return existing
         
        dup= None
        if self.parent:
            dup= PathPart(self.name, deepcopy(self.parent, memo), deepcopy(self.PathStatus, memo), deepcopy(self.depth, memo))
        else:
            dup= PathPart(self.name, None, deepcopy(self.PathStatus, memo), deepcopy(self.depth, memo))
        
        return dup
        
class ConfigLayer(pod.Object):
    def __init__(self, name, FileAccess, lPathStatus, parent=None):
        pod.Object.__init__(self)
        
        self.name= name
        self.parent= parent
        self.children= []
        if parent:
            self.parent.children.append(self)
        self.FileAccess= FileAccess
        self.paths= PathPart("root")
        self.paths.PathStatus= lPathStatus
        
    def PathExists(self, lpath):
        path= self.paths
        while path:
            if path.PathExists(lpath):
                return True
            path= self.paths.parent
        
        return False
    
    def GetPathStatus(self, path, report_truncated= False, previous_truncated=None):
        depth= len(path)
        if report_truncated:
            (previous_truncated, tpathPart)= self.paths.GetLastPart(path, report_truncated)
        else:
            tpathPart= self.paths.GetLastPart(path)
        if(tpathPart.PathStatus==PathStatus.stop):
            if report_truncated:
                return (False,PathStatus.stop)
            else:
                return PathStatus.stop
        if(tpathPart.PathStatus==PathStatus.undef):
            return self.__GetPathStatus__(path, self, self.paths, depth, report_truncated, previous_truncated)
        else:
            return self.__GetPathStatus__(path, self, tpathPart, depth, report_truncated, previous_truncated)
    
    def __GetPathStatus__(self, path, parent, previousBest, depth, report_truncated= False, previous_truncated=None):
        if(parent.parent==None):
            if report_truncated:
                return (previous_truncated,previousBest.PathStatus)
            else:
                return previousBest.PathStatus
        
        if report_truncated:
            (truncated, tpathPart)= parent.parent.paths.GetLastPart(path, report_truncated)
        else:
            tpathPart= parent.parent.paths.GetLastPart(path)
        
        # If we must stop at speciffic path then return
        # status to stop regardless if parent paths have
        # any other plans.
        if(tpathPart.PathStatus==PathStatus.stop):
            if report_truncated:
                return (truncated, PathStatus.stop)
            else:
                return PathStatus.stop
        
        # If path status is not defined go search to the next parent.
        if(tpathPart.PathStatus==PathStatus.undef):
            return self.__GetPathStatus__(path, parent.parent, previousBest, depth, report_truncated, previous_truncated)

        # Get the difference betwene path length and previousBest depth and current one.
        sum1= depth-previousBest.depth
        sum2= depth-tpathPart.depth
        
        # If new path is closer store new one.
        # WARNING: This is overlaping mode and should
        #          be avoided, when creating config.
        # TODO:    Add option to enable and disable
        #          overlaping mode for speciffic path.
        if( sum2>0 and sum2<sum1):
            previousBest= tpathPart
            if report_truncated:
                previous_truncated= truncated
        
        if(parent.parent.parent==None):
            if report_truncated:
                return (previous_truncated, previousBest.PathStatus)
            else:
                return previousBest.PathStatus;
        
        return self.__GetPathStatus__(path, parent.parent, previousBest, depth, report_truncated, previous_truncated)

    def GetConfigByPath(self, path):
        if self.name != path[0]:
            return None
        
        if len(path)>1:
            for child in self.children:
                if child.GetConfigByPath(path[1:]):
                    return child
        else:
            return self
        
        return None
    
    def GetConfigByName(self, name):
        if self.name != name:
            if self.children:
                for child in self.children:
                    cfg= child.GetConfigByName(name)
                    if cfg: return cfg
            else:
                return None
        
        return self
    
    def GetRootConfigLayer(self):
        parent= self.parent
        last_parent= self
        while parent:
            last_parent= parent
            parent= parent.parent
            
        return last_parent
        
    def pre_delete(self):
        for child in self.children:
            self.children.remove(child)
            child.delete()
        
        self.paths.delete()
        
    def __deepcopy__(self, memo):
        not_there = []
        existing = memo.get(self, not_there)
        if existing is not not_there:
            return existing
         
        dup= None
        if self.parent:
            dup= ConfigLayer(self.name+"_copy", None, deepcopy(self.paths.PathStatus, memo), deepcopy(self.parent, memo))
        else:
            dup= ConfigLayer(self.name+"_copy", None, deepcopy(self.paths.PathStatus, memo), None)
            
        dup.paths= deepcopy(self.paths, memo)
            
        return dup
        
class ConfigLayerManager(pod.Object):
    '''
    Stores layer configurations.
    All created configurations goes here, so they can be reusable in
    many FileSyncConfig-s. Config layers should never be deleted from
    FileSyncConfig, because it's just the object who uses them.
    '''
    
    def __init__(self):
        pod.Object.__init__(self)
        
        self.configs= []
        
    def RootAdd(self, config):
        root= config.GetRootConfigLayer()
        if root not in self.configs:
            self.configs.append(root)        
        
    def NewConfig(self, *args, **kwargs):
        '''
        Adds new config layer, same parameters as ConfigLayer
        '''
        config = ConfigLayer(*args, **kwargs)
        self.RootAdd(config)
        
        return config
        
    def AddConfig(self, *configs):
        '''
        Adds config layers
        @param config: Multiple configs
        @type config: ConfigLayer[]
        '''
        for config in configs:
            #Dont add config if config with the same name exists
            if self.GetConfigByName(config.name):
                return False
            self.RootAdd(config)
            
        return True
        
    def GetConfigByName(self, name):
        '''
        Gets config my name.
        @param name: Config name
        @type name: Stringself
        '''
        if name==None:
            return None
        
        name_parts= name.split("->")
        
        possible_cfg= None
        for config in self.configs:
            cfg= config.GetConfigByPath(name_parts)
            if cfg:
                return cfg
            # we search for fist occurence
            elif len(name_parts)==1 and not possible_cfg:
                possible_cfg= config.GetConfigByName(name)
        
        return possible_cfg
    
    def DuplicateConfig(self, config):
        dup= deepcopy(config)
        self.AddConfig(dup)
        
    def RemoveConfig(self, config):
        '''
        Removes config layer. Also removes all sub-config layers
        that depend on this config.
        @param config: Configuration
        @type config: ConfigLayer
        '''       
        if not config: return 
        config= config.GetRootConfigLayer()
        if config in self.configs:
            self.configs.remove(config)
            config.delete()
            
    def GetRootConfigs(self):
        return self.configs
        
    def pre_delete(self):
        '''
        Erases all config layers.
        '''
        for config in self.configs:
            self.configs.remove(config)
            config.delete()
        
class FileIndex(pod.Object):
    name = pod.typed.String(index = True) 
    CreationTime= pod.typed.Time(index = False)
    
    def __init__(self, name, parent= None, CreationTime= None):
        pod.Object.__init__(self)
        
        self.name= name
        self.CreationTime= CreationTime    
        
        self.children= pod.list.List()
        self.parent= parent
        if parent!=None:
            self.parent.children.append(self)
                
    def AddPath(self, path, CreationTime= None):
        if(path==[]):
            return self
        
        tpathPart=FileIndex(path[0], self, CreationTime)
        
        return tpathPart.AddPath(path[1:])   
        
    def GetPathPart(self, path, new=False):
        if(path==[]):
            return self.parent
        if(self.name!=path[0]):
            return None
            
        for child in self.children:
            tpathPart= child.GetPathPart(path[1:], new)
            if(tpathPart!=None):
                return tpathPart
            
        if(new == True):
            return self.AddPath(path[1:])
        
        return self
        
    def DelPathPart(self, path):
        tpathPart= self.GetPathPart(path, False)
        if(tpathPart==None):
            return
        
        tpathPart.parent.children.remove(tpathPart)
        tpathPart.delete()
    
    def __str__(self):
        absolute_path= '/'.join(self.AbsolutePath())
        return absolute_path + " [" + self.CreationTime + "]"
    
    def AbsolutePath(self):
        path=[self.name]
        parent= self.parent
        while parent:
            path.append(parent.name)
            parent= parent.parent
        path.reverse()
        return path 
        
    def pre_delete(self):
        for child in self.children:
            self.children.remove(child)
            child.delete()

        self.children.delete()
        
    def __deepcopy__(self, memo):
        not_there = []
        existing = memo.get(self, not_there)
        if existing is not not_there:
            return existing
         
        dup= None
        if self.parent:
            dup= FileIndex(self.name, deepcopy(self.parent, memo), deepcopy(self.CreationTime, memo), deepcopy(self.depth, memo))
        else:
            dup= FileIndex(self.name, None, deepcopy(self.CreationTime, memo), deepcopy(self.depth, memo))
            
        return dup
        
class FileSyncConfig(pod.Object):
    def __init__(self, source_path, dest_path, config_layer, name=None):
        '''
        init
        @param source_path: Path to source used by pyfileaccess fsopendir
        @type source_path: String
        @param dest_path: Path to dest used by pyfileaccess fsopendir
        @type dest_path: String
        @param config_layer: Config layer to use
        @type config_layer: ConfigLayer
        '''
        pod.Object.__init__(self)
        
        self.source_path = source_path
        self.dest_path = dest_path
        self.config_layer = config_layer
        self.name= name
        
        self.src_index= FileIndex("root")
        self.dst_index= FileIndex("root")
        
    def ClearIndexes(self):
        self.src_index.delete()
        self.dst_index.delete()  
        self.src_index= FileIndex("root")
        self.dst_index= FileIndex("root") 
        
    def __str__(self):
        return "name:'%s', src:'%s', dst:'%s', config:'%s'" \
             % (self.name, self.source_path, self.dest_path, self.config_layer.name)
        
class FileSyncConfigManager(pod.Object):
    def __init__(self):
        pod.Object.__init__(self)
        self.configs= []
        
    def AddConfig(self, config):
        if self.GetConfigByName(config.name):
            return False
        self.configs.append(config)
        
        return True
        
    def RemoveConfig(self, config):
        self.configs.remove(config)
        config.delete()
        
    def GetConfigByName(self, name):
        for config in self.configs:
            if config.name==name:
                return config
        
class FileSync(object):
    def __init__(self, file_sync_config, db= None):
        '''
        init
        @param file_sync_config: Confg to use with synch.
        @type file_sync_config: FileSyncConfig
        '''
        self.file_sync_config = file_sync_config
        self.cache_file_status= True
        self.db= db
        
    def SmallTime(self, time1, time2):
        if abs(time1 - time2)<timedelta(seconds=1):
            return True
        
        return False
        
    def sync(self, base_path= ["root"], verbose=True):
        self.start_time= datetime.now()
        
        if(self.file_sync_config.source_path):
            try:
                src= fsopendir(self.file_sync_config.source_path)
            except fs.opener.OpenerError, fs.opener.NoOpenerError:
                print "Error opening %s!" % (self.file_sync_config.source_path)
        else:
            return
        if(self.file_sync_config.dest_path):
            try:
                dst= fsopendir(self.file_sync_config.dest_path)
            except fs.opener.OpenerError, fs.opener.NoOpenerError:
                print "Error opening %s!" % (self.file_sync_config.dest_path)
        else:
            return
        
        #Make config as global var
        self.config= self.file_sync_config.config_layer
        self._synch_walk(src, dst, base_path, self.file_sync_config.src_index.GetPathPart(base_path, True), self.file_sync_config.dst_index.GetPathPart(base_path, True) )
        
    def dt2ut(self, date):
        return int(mktime(date.timetuple()))
    
    def ut2dt(self, date):
        return datetime.fromtimestamp(date)
        
    def _synch_walk(self, src, dst, path, src_i, dst_i, depth= 0, cached_status= None, verbose=True):
        #Get list of files in dirs        
        src_files= src.listdir()
        dst_files= dst.listdir()
        
        #This operations are considered slow,
        #so we want to do them only once.
        src_info= [src.getinfo(i) for i in src_files]
        dst_info= [dst.getinfo(i) for i in dst_files]
        
        src_f= []
        src_d= []
        src_l=[]
        for id, i in enumerate(src_files):
            if is_file( src_info[id] ): src_f.append( (i,src_info[id]) )
            elif is_dir( src_info[id] ): src_d.append( (i,src_info[id]) )
            elif is_lnk( src_info[id] ): src_l.append( (i,src_info[id]) )
            else: pass #If file/dir is something we don't know we just pass.
            
        dst_f= []
        dst_d= []
        dst_l=[]
        for id, i in enumerate(dst_files):
            if is_file( dst_info[id] ): dst_f.append( (i,dst_info[id]) )
            elif is_dir( dst_info[id] ): dst_d.append( (i,dst_info[id]) )
            elif is_lnk( dst_info[id] ): dst_l.append( (i,dst_info[id]) )
            else: pass #If file/dir is something we don't know we just pass.

        #This operations are considered fast
        #Src
        copy_src_files = [(i,info) for i, info in src_f if i not in dst_files or (i, info) in dst_d]
        copy_src_dirs = [(i, info) for i, info in src_d if i not in dst_files or (i, info) in dst_f]
        make_src_links = [(i,info) for i, info in src_l if i not in dst_files]
        #Dst
        copy_dst_files = [(i,info) for i, info in dst_f if i not in src_files or (i, info) in src_d]
        copy_dst_dirs = [i for i, info in dst_d if i not in src_files or (i, info) in src_f]
        make_dst_links = [(i,info) for i, info in dst_l if i not in src_files]
        
        #Update files are in src and dst the same.
        #Files that are not in those files we are about to copy.
        update_files=[]
        for i1, sinfo in src_f:
            if (i1, sinfo) not in copy_src_files:
                for i2, dinfo in dst_f:
                    if i1==i2: update_files.append((i1, sinfo, dinfo))
        print update_files
        #Dirs that are not in those dirs we are about to copy.
        update_dirs=[]
        for i1, sinfo in src_d:
            if (i1, dinfo) not in copy_src_dirs: update_dirs.append((i1,sinfo,dinfo))
        update_links=[]
        for i1, sinfo in src_l:
            if (i1, sinfo) not in make_src_links:
                for i2, dinfo in dst_l:
                    if i1==i2: update_links.append((i1, sinfo, dinfo))
                
        #Select truncated based on if we have chached_status or not,
        #this way we don't have to pass another variable around.
        if cached_status: truncated= True
        else: truncated= False
        
        #Do all the hard work.
        #src->dst
        self._copy_files(src, dst, path, src_i, dst_i, copy_src_files, truncated, depth, cached_status, verbose)
        self._copy_dirs(src, dst, path, src_i, dst_i, copy_src_dirs, truncated, depth, cached_status, verbose)
        self._make_links(src, dst, path, src_i, dst_i, make_src_links, truncated, depth, cached_status, verbose)
        #dst->src
        self._copy_files(dst, src, path, dst_i, src_i, copy_dst_files, truncated, depth, cached_status, verbose)
        self._copy_dirs(dst, src, path, dst_i, src_i, copy_dst_dirs, truncated, depth, cached_status, verbose)
        self._make_links(dst, src, path, dst_i, src_i, make_dst_links, truncated, depth, cached_status, verbose)
        #dst<->src
        self._update_files(src, dst, path, src_i, dst_i, update_files, truncated, depth, cached_status, verbose)
        self._update_dirs(src, dst, path, src_i, dst_i, update_dirs, truncated, depth, cached_status, verbose)
        self._update_links(src, dst, path, src_i, dst_i, update_links, truncated, depth, cached_status, verbose)
        #We have to check if permissions have been changed on files links and dirs.
        self._update_permissions(src, dst, update_files+update_dirs+update_links, truncated, depth, cached_status, verbose)
        
        #Save file indexes to database from time to time.
        if datetime.now()-self.start_time>timedelta(seconds=100):
            print "Commit CommitCommitCommitCommitCommitCommitCommitCommitCommitCommitCommit"
            self.db.commit()
            self.start_time= datetime.now()
            
    def _copy_files(self, src, dst, path, src_i, dst_i, files, truncated= False, depth= 0, cached_status= None,verbose=True):
        if verbose and files: print "\t"*depth+"Copy files"
        status= cached_status
        for file, info in files:
            if verbose: print "\t"*depth+"Object: "+file
            if not cached_status or cached_status==PathStatus.ignore or not self.cache_file_status:
                status= self.config.GetPathStatus(path+[file])
            if status==PathStatus.include:
                src_index= None
                dst_index= None
                src_mtime= info["modified_time"]
                src_filesize= info["size"]
                
                try:
                    copyfile(src, file, dst, file)
                    #Change mod of newly created file
                    #to mod of a source file
                    dst.chmod(file, get_fmod(info))
                except:
                    continue
                if src_filesize>=0:
                    print "Sync synch synch..............................."
                    src_index= src_i.GetPathPart([src_i.name,file], True)
                    dst_index= dst_i.GetPathPart([dst_i.name,file], True)
                    src_index.CreationTime= self.dt2ut(src_mtime)
                    dst_index.CreationTime= self.dt2ut(dst.getinfo(file)["modified_time"])   
            if status==PathStatus.stop:
                if verbose: print "\t"*depth+"Removing file"
                src.remove(file)    
                
    def _copy_dirs(self, src, dst, path, src_i, dst_i, dirs, truncated= False, depth= 0, cached_status= None,verbose=True):
        if verbose and dirs: print "\t"*depth+"Copy dirs"
        status= cached_status
        for file, sinfo, dinfo in dirs:
            if verbose: print "\t"*depth+"Object: "+file
            l_cached_status= None
            if not cached_status:
                (truncated, status)= self.config.GetPathStatus(path+[file], True)
            if truncated:
                l_cached_status= status
            if status==PathStatus.include or (status==PathStatus.ignore and self.config.PathExists(path+[file])):
                if verbose: print "\t"*depth+"dir_enter->"
                new_src= src.makeopendir(file)
                new_dst= dst.makeopendir(file)
                #Change mod of newly created file
                #to mod of a source file
                dst.chmod(file, get_fmod(sinfo))
                self._synch_walk(new_src, new_dst, path[:]+[file], src_i.GetPathPart([src_i.name,file], True), dst_i.GetPathPart([dst_i.name,file], True), depth+1, l_cached_status)
                if verbose: print "\t"*depth+"<-dir_leave"
            elif status==PathStatus.stop:
                if verbose: print "\t"*depth+"Removing dir"
                src.removedir(file, force=True)
            else:
                pass
                
    def _update_files(self, src, dst, path, src_i, dst_i, files, truncated= False, depth= 0, cached_status= None,verbose=True):
        if verbose and files: print "\t"*depth+"Update files"
        status= cached_status
        for file, sinfo, dinfo in files:
            if verbose: print "\t"*depth+"Object: "+file
            if not cached_status or not self.cache_file_status:
                (truncated, status)= self.config.GetPathStatus(path+[file], True)
                
            if status==PathStatus.include:
                src_index= None
                dst_index= None
                src_mtime= sinfo["modified_time"]
                src_filesize= src.getsize(file)
                dst_mtime= dinfo["modified_time"]
                dst_filesize= dst.getsize(file)
                
                if verbose: print "\t"*depth+"Synching file"
                if src_filesize>1000 or dst_filesize>1000:
                    src_index= src_i.GetPathPart([src_i.name,file], True)
                    dst_index= dst_i.GetPathPart([dst_i.name,file], True)
                #If we get error when getting indexes, just action based on scr or dst mtime
                if src_index==None or dst_index==None:
                    if src_mtime>dst_mtime:
                        try:
                            copyfile(src, file, dst, file)
                        except:
                            continue
                    else:
                        try:
                            copyfile(dst, file, src, file)
                        except:
                            continue
                #Create index time, if it does not exist yet.
                elif src_index.CreationTime==None or dst_index.CreationTime==None:
                    if verbose: print "\t"*depth+"No index time found."
                    if src_mtime>dst_mtime:
                        try:
                            copyfile(src, file, dst, file)
                        except:
                            continue
                        src_index.CreationTime= self.dt2ut(src_mtime)
                        dst_index.CreationTime= self.dt2ut(dst.getinfo(file)["modified_time"])
                    else:
                        try:
                            copyfile(dst, file, src, file)
                        except:
                            continue
                        dst_index.CreationTime= self.dt2ut(dst_mtime)
                        src_index.CreationTime= self.dt2ut(src.getinfo(file)["modified_time"])
                #When indexes exist
                else:    
                    #both files are unchanged
                    if self.SmallTime(src_mtime, self.ut2dt(src_index.CreationTime)) and self.SmallTime(dst_mtime, self.ut2dt(dst_index.CreationTime)):
                        if verbose: print "\t"*depth+"Both files are synched"
                    #src has changed and dst has not
                    elif self.ut2dt(src_index.CreationTime)<src_mtime and self.SmallTime(dst_mtime, self.ut2dt(dst_index.CreationTime)):
                        if verbose: print "\t"*depth+"Src file has changed, but dst not"
                        try:
                            copyfile(src, file, dst, file)
                        except:
                            continue
                        dst_index.CreationTime= self.dt2ut(dst.getinfo(file)["modified_time"])
                    #dst has changed and src has not
                    elif self.ut2dt(dst_index.CreationTime)<dst_mtime and self.SmallTime(src_mtime, self.ut2dt(src_index.CreationTime)):
                        if verbose: print "\t"*depth+"Dst file has changed, but src not"
                        try:
                            copyfile(dst, file, src, file)
                        except:
                            continue
                        src_index.CreationTime= self.dt2ut(src.getinfo(file)["modified_time"])
                    #both files has changed, update indexes 
                    elif not self.SmallTime(src_mtime, self.ut2dt(src_index.CreationTime)) and not self.SmallTime(dst_mtime, self.ut2dt(dst_index.CreationTime)):
                        if verbose: print "\t"*depth+"Both files has changed."
                        if src_mtime>dst_mtime:
                            try:
                                copyfile(src, file, dst, file)
                            except:
                                continue
                            src_index.CreationTime= self.dt2ut(src_mtime)
                            dst_index.CreationTime= self.dt2ut(dst.getinfo(file)["modified_time"])
                        else:
                            try:
                                copyfile(dst, file, src, file)
                            except:
                                continue
                            dst_index.CreationTime= self.dt2ut(dst_mtime)
                            src_index.CreationTime= self.dt2ut(src.getinfo(file)["modified_time"])
            #If we have stop on file just delete it on both sides
            if status==PathStatus.stop:
                if verbose: print "\t"*depth+"Removing file"
                src.remove(file)
                dst.remove(file)
                            
    def _update_dirs(self, src, dst, path, src_i, dst_i, dirs, truncated= False,depth= 0, cached_status= None,verbose=True):
        if verbose and dirs: print "\t"*depth+"Update dirs"
        status= cached_status
        for file in dirs:
            if verbose: print "\t"*depth+"Object: "+file
            if not cached_status or not self.cache_file_status:
                (truncated, status)= self.config.GetPathStatus(path+[file], True)
            if truncated:
                cached_status= status
            if status==PathStatus.include or (status==PathStatus.ignore and self.config.PathExists(path+[file])):
                if verbose: print "\t"*depth+"dir_enter->"
                new_src= src.makeopendir(file)
                new_dst= dst.makeopendir(file)
                self._synch_walk(new_src, new_dst, path[:]+[file], src_i.GetPathPart([src_i.name,file], True), dst_i.GetPathPart([dst_i.name,file], True), depth+1, cached_status)
                if verbose: print "\t"*depth+"<-dir_leave"
            elif status==PathStatus.stop:
                if verbose: print "\t"*depth+"Removing dir"
                src.removedir(file, force=True) 
                dst.removedir(file, force=True)
                
    def _make_links(self, src, dst, path, src_i, dst_i, links, truncated= False,depth= 0, cached_status= None,verbose=True):
        if verbose and links: print "\t"*depth+"Make links"
        status= cached_status
        for link, info in links:
            if verbose: print "\t"*depth+"Object: "+link
            if not cached_status or not self.cache_file_status:
                (truncated, status)= self.config.GetPathStatus(path+[link], True)
            if truncated:
                cached_status= status
            if status==PathStatus.include:
                lnk= src.readlink(link)
                if verbose: print "\t"*depth+"Creating link to"+lnk
                dst.symlink(lnk, link)
                #Change mod of newly created link
                #to mod of a source link
                dst.chmod(link, get_fmod(info))
            elif status==PathStatus.stop:
                if verbose: print "\t"*depth+"Removing link"
                src.remove(link)
                
    def _update_permissions(self, src, dst, files, truncated= False,depth= 0, cached_status= None,verbose=True):):
        if verbose and links: print "\t"*depth+"Update permissions"
        
        #Here we can't act based on modification time,
        #so we have to decide based on options.
        for file, sinfo, dinfo in files:
            if verbose: print "\t"*depth+"Object: "+file
            #We update in case if st_modes are different
            #this should be sufficient detection.
            if get_fmod(sinfo["st_mode"])!=get_fmod(dinfo["st_mode"]):
                dst.chmod(file, get_fmod(sinfo))
                #src.chmod(file, dmod)
                
    def _update_links(self, src, dst, path, src_i, dst_i, links, truncated= False,depth= 0, cached_status= None,verbose=True):
        if verbose and links: print "\t"*depth+"Update links"
        status= cached_status
        for file, sinfo, dinfo in links:
            if verbose: print "\t"*depth+"Object: "+file
            if not cached_status or not self.cache_file_status:
                (truncated, status)= self.config.GetPathStatus(path+[file], True)
            if truncated:
                cached_status= status
            if status==PathStatus.include:
                #Links point to same location, do nothing.
                slnk= src.readlink(file)
                dlnk= dst.readlink(file)
                if slnk==dlnk:
                    if verbose: print "\t"*depth+"Links are in synch" 
                    continue
                
                #In case links are different use
                src_index= None
                dst_index= None
                src_mtime= sinfo["modified_time"]
                dst_mtime= dinfo["modified_time"]
                
                if verbose: print "\t"*depth+"Synching links"
                src_index= src_i.GetPathPart([src_i.name,file], True)
                dst_index= dst_i.GetPathPart([dst_i.name,file], True)
                #If we get error when getting indexes, just action based on scr or dst mtime
                if src_index==None or dst_index==None:
                    if src_mtime>dst_mtime:
                        try:
                            dst.remove(file)
                            dst.symlink(slnk, file)
                        except: continue
                    else:
                        try:
                            src.remove(file)
                            src.symlink(dlnk, file)
                        except: continue
                #Create index time, if it does not exist yet.
                elif src_index.CreationTime==None or dst_index.CreationTime==None:
                    if verbose: print "\t"*depth+"No index time found."
                    if src_mtime>dst_mtime:
                        try:
                            dst.remove(file)
                            dst.symlink(slnk, file)
                        except: continue
                        src_index.CreationTime= self.dt2ut(src_mtime)
                        dst_index.CreationTime= self.dt2ut(dst.getinfo(file)["modified_time"])
                    else:
                        try:
                            src.remove(file)
                            src.symlink(dlnk, file)
                        except: continue
                        dst_index.CreationTime= self.dt2ut(dst_mtime)
                        src_index.CreationTime= self.dt2ut(src.getinfo(file)["modified_time"])
                #When indexes exist
                else:    
                    #both files are unchanged
                    if self.SmallTime(src_mtime, self.ut2dt(src_index.CreationTime)) and self.SmallTime(dst_mtime, self.ut2dt(dst_index.CreationTime)):
                        if verbose: print "\t"*depth+"Links were created at the same time, use one of them."
                        #We must copy one of the links.
                        dst.remove(file)
                        dst.symlink(slnk, file)
                    #src has changed and dst has not
                    elif self.ut2dt(src_index.CreationTime)<src_mtime and self.SmallTime(dst_mtime, self.ut2dt(dst_index.CreationTime)):
                        if verbose: print "\t"*depth+"Src link has changed, but dst not"
                        try:
                            dst.remove(file)
                            dst.symlink(slnk, file)
                        except: continue
                        dst_index.CreationTime= self.dt2ut(dst.getinfo(file)["modified_time"])
                    #dst has changed and src has not
                    elif self.ut2dt(dst_index.CreationTime)<dst_mtime and self.SmallTime(src_mtime, self.ut2dt(src_index.CreationTime)):
                        if verbose: print "\t"*depth+"Dst link has changed, but src not"
                        try:
                            src.remove(file)
                            src.symlink(dlnk, file)
                        except: continue
                        src_index.CreationTime= self.dt2ut(src.getinfo(file)["modified_time"])
                    #both files has changed, update indexes 
                    elif not self.SmallTime(src_mtime, self.ut2dt(src_index.CreationTime)) and not self.SmallTime(dst_mtime, self.ut2dt(dst_index.CreationTime)):
                        if verbose: print "\t"*depth+"Both links has changed."
                        if src_mtime>dst_mtime:
                            try:
                                dst.remove(file)
                                dst.symlink(slnk, file)
                            except: continue
                            src_index.CreationTime= self.dt2ut(src_mtime)
                            dst_index.CreationTime= self.dt2ut(dst.getinfo(file)["modified_time"])
                        else:
                            try:
                                src.remove(file)
                                src.symlink(dlnk, file)
                            except: continue
                            dst_index.CreationTime= self.dt2ut(dst_mtime)
                            src_index.CreationTime= self.dt2ut(src.getinfo(file)["modified_time"])
            #if we have stop on link just delete it on both sides
            elif status==PathStatus.stop:
                if verbose: print "\t"*depth+"Removing link"
                src.remove(file)
                dst.remove(file)
