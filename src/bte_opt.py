from scipy.sparse.linalg import spsolve
import os,sys
import numpy as np 
from scipy.sparse import csc_matrix
from pyvtk import *
import numpy as np
from mpi4py import MPI
import shutil
from scipy.sparse.linalg import *
from scipy.sparse import diags
from scipy.io import *
import scipy.io
import h5py
from utils import *
import deepdish as dd


class BTE_OPT(object):
  
  def __init__(self,argv):

   self.mesh = argv['geometry']
   self.x = argv.setdefault('density',np.ones(len(self.mesh.elems)))

   #Get material properties-----
   mat = argv['material']
   self.B0 = mat.state['B0']
   self.B1 = mat.state['B1']
   self.B2 = mat.state['B2']
   self.kappa_bulk = mat.state['kappa_bulk_tot']

   mat = argv.setdefault('secondary_material',argv['material'])
   self.B0_2 = mat.state['B0']
   self.B1_2 = mat.state['B1']
   self.B2_2 = mat.state['B2']
  
   self.compute_gradient = argv.setdefault('compute_gradient',False)



   self.mfp = np.array(mat.state['mfp_sampled'])/1e-9 #In nm
   self.n_mfp = len(self.B0)

   #INITIALIZATION-------------------------
   self.n_el = len(self.mesh.elems)
   data = self.mesh.compute_boundary_condition_data('x')
   self.side_periodic_value = data['side_periodic_value']
   self.flux_sides = data['flux_sides']
   self.area_flux = data['area_flux']
   #self.compute_matrix = argv['compute_matrix']
   self.compute_matrix = True
   self.sequence = []
   self.kappa = []
   self.dom = mat.state['dom']
   self.n_theta = self.dom['n_theta']
   self.n_phi = self.dom['n_phi']

   self.n_elems = len(self.mesh.elems)

   self.compute_connection_matrix()
   if self.compute_matrix:
    if MPI.COMM_WORLD.Get_rank() == 0:
     directory = 'tmp'
     if os.path.exists(directory):
      shutil.rmtree(directory)
     os.makedirs(directory)
  
    #compute matrix--------
    output = compute_sum(self.compute_directional_connections,self.n_phi,{},{})
   

   #solve the BTE 
   if MPI.COMM_WORLD.Get_rank() == 0:
    print('Solving BTE... started.')
    print('')
   self.compute_function(**argv)

   if MPI.COMM_WORLD.Get_rank() == 0:
    print('')
    print('Solving BTE....done.')
    print('')
    print('Effective Thermal Conductivity (BTE):   ' + str(round(self.state['bte_kappa'],4)) + r' W/m/K')
    print('')

   #if argv['compute_gradient']:
   # self.compute_gradient()



   if MPI.COMM_WORLD.Get_rank() == 0:
    if os.path.isdir('tmp'):
     shutil.rmtree('tmp')




   #SAVE FILE--------------------
   if argv.setdefault('save',True):
    if MPI.COMM_WORLD.Get_rank() == 0:
     dd.io.save('solver.hdf5', self.state)


  def get_transmission(self,i,j):

   a = 2.0*self.x[i]*self.x[j]/(self.x[i]+self.x[j])
   #return 1.0

   return a

  def compute_directional_connections(self,p,options):
    
   
   Bplus = []
   rplus = []
   cplus = []
   Bminus = []
   rminus = []
   cminus = []
   Fminus = []
   Fplus = []
   
   for i,j in zip(*self.B.nonzero()):
   
    side = self.mesh.get_side_between_two_elements(i,j)  
    normal = self.mesh.compute_side_normal(i,side)
    
    tmp = np.dot(normal,self.dom['phi_dir'][p])
    if tmp > 0:
     Bplus.append(1)
     rplus.append(i)
     cplus.append(j)
     v = self.mesh.get_side_periodic_value(side,i,self.side_periodic_value)
     Fplus.append(v)

    if tmp < 0 :
     Bminus.append(1)
     rminus.append(i)
     cminus.append(j)
     v = self.mesh.get_side_periodic_value(side,i,self.side_periodic_value)
     Fminus.append(v)

   #--------------------------WRITE FILES---------------------
   Bp = csc_matrix( (Bplus,(rplus,cplus)), shape=(self.n_elems,self.n_elems) )
   scipy.io.mmwrite('tmp/BPLUS_' + str(p) + r'.mtx',Bp) 
   Bm = csc_matrix( (Bminus,(rminus,cminus)), shape=(self.n_elems,self.n_elems) )
   scipy.io.mmwrite('tmp/BMINUS_'+ str(p) + r'.mtx',Bm) 
   Fm = csc_matrix( (Fminus,(rminus,cminus)), shape=(self.n_elems,self.n_elems) )
   scipy.io.mmwrite('tmp/FMINUS_' + str(p) + r'.mtx',Fm) 
   Fp = csc_matrix( (Fplus,(rplus,cplus)), shape=(self.n_elems,self.n_elems) )
   scipy.io.mmwrite('tmp/FPLUS_' + str(p) + r'.mtx',Fp) 
   #np.save(file('tmp/F_' + str(p),'w+'),F)
   #----------------------------------------------------------

  def compute_connection_matrix(self) :
   
   nc = len(self.mesh.elems)
   row_tmp = []
   col_tmp = []
   data_tmp = [] 
   data_tmp_b = [] 

   for ll in self.mesh.side_list['active'] :
    if not ll in self.mesh.side_list['Boundary']:
     area = self.mesh.get_side_area(ll)
     elems = self.mesh.get_elems_from_side(ll)
     kc1 = elems[0]
     c1 = self.mesh.get_elem_centroid(kc1)
     normal = self.mesh.get_side_normal(0,ll)
     Af = area*normal  
     nodes = self.mesh.side_node_map[ll]
     c2 = self.mesh.get_next_elem_centroid(kc1,ll)
     kc2 = elems[1]  
     row_tmp.append(kc1)
     col_tmp.append(kc2)
     data_tmp.append(1.0)
     row_tmp.append(kc2)
     col_tmp.append(kc1)
     data_tmp.append(1.0)
  
   self.B = csc_matrix( (np.array(data_tmp),(np.array(row_tmp),np.array(col_tmp))), shape=(nc,nc) )


  def solve(self,p,options):

   kappa = 0.0
   DS = csc_matrix(options['DS'])
   old_temp = options['temp']
   TB = options['boundary_temp']
   Bminus=scipy.io.mmread('tmp/BMINUS_' + str(p) + '.mtx').tocsc() 
   Bplus=scipy.io.mmread('tmp/BPLUS_' + str(p) + '.mtx').tocsc()
   Fminus=scipy.io.mmread('tmp/FMINUS_' + str(p) + '.mtx').tocsc() 
   Fplus=scipy.io.mmread('tmp/FPLUS_' + str(p) + '.mtx').tocsc() 
   phi_factor = self.dom['phi_dir'][p]/self.dom['d_phi_vec'][p]
   #-------------------------------------------------------------------------
  
   new_temp = np.zeros(self.n_el) 
   flux = np.zeros((self.n_el,3)) 
   new_boundary_temp = np.zeros(self.n_el) 
   suppression = np.zeros((self.n_mfp,self.n_theta,self.n_phi))
   rd = [];cd = [];dd = [] #initialize for diffuse scattering
   symmetry = 2
   kappa = 0

   kappa_factor = 3.0/4.0/np.pi*self.mesh.size[0]/self.area_flux
   for t in range(int(self.n_theta/2.0)): #We take into account the symmetry
    theta_factor = self.dom['at'][t] / self.dom['d_theta_vec'][t]
    angle_factor = phi_factor * theta_factor

    #Assemble Am----------------
    r = [];c = [];d = [] 
    C = np.zeros(self.n_elems)
    for i,j in zip(*Bminus.nonzero()):
     r.append(i);c.append(j)
     tmp = np.dot(angle_factor,self.mesh.get_coeff(i,j))
     a = self.get_transmission(i,j)
     value = tmp*a
     d.append(value)
     C[i] -= DS[i,j]*(1.0-a)*tmp #Diffuse scattering (interfaces)
    Am = csc_matrix( (np.array(d),(np.array(r),np.array(c))), shape=(self.n_elems,self.n_elems) )

    #Assemble Ap----------------
    r = [];c = [];d = [] 
    for i,j in zip(*Bplus.nonzero()):
     r.append(i);c.append(j)
     tmp = np.dot(angle_factor,self.mesh.get_coeff(i,j))
     value = tmp
     d.append(value)
    Ap = csc_matrix( (np.array(d),(np.array(r),np.array(c))), shape=(self.n_elems,self.n_elems) )

    #-----------------------------------------
    #PERIODICITY------
    B = np.zeros(self.n_elems)
    for i,j in zip(*Fminus.nonzero()):
     a = self.get_transmission(i,j)
     tmp = np.dot(angle_factor,self.mesh.get_coeff(i,j))
     value = tmp*a
     B[i] += Fminus[i,j] * value
     C[i] -= DS[i,j]*(1.0-a)*tmp #Diffuse scattering (interfaces)

 
    #------------------------------------
     
    #MFP space
    for m in range(self.n_mfp):
     #solve the system---
     b = [tmp[0,0] for tmp in Ap.sum(axis=1)]
     F = scipy.sparse.eye(self.n_elems) + self.mfp[m] * diags([b],[0]).tocsc() + self.mfp[m] * Am 

     #Compute gradient---------------------
     dg_dT = np.zeros(self.n_elems)
     if self.compute_gradient:
      for i,j in zip(*Fminus.nonzero()):
       a = self.get_transmission(i,j)
       dg_dT[i] += self.B0[m]*kappa_factor/2.0*Fplus[i,j]*a*np.dot(self.dom['S'][t][p],self.mesh.get_af(i,j))/self.mfp[m]*2.0

      for i,j in zip(*Fminus.nonzero()):
       dg_dT[i] += self.B0[m]*kappa_factor/2.0*Fminus[i,j]*np.dot(self.dom['S'][t][p],self.mesh.get_af(i,j))/self.mfp[m]*2.0


     #RHS = old_temp + self.mfp[m]*B + self.mfp[m]*C + dg_dT
     RHS = old_temp + dg_dT


     temp = spsolve(F,RHS,use_umfpack = True)
     #-------------------------------------------------
      
     #For diffuse scattering--------------
     for i,j in zip(*Bplus.nonzero()):
      rd.append(i); cd.append(j) 
      value = self.B1[m]*symmetry*temp[i]*np.dot(self.dom['S'][t][p],self.mesh.get_normal_between_elems(i,j))/np.pi
      dd.append(value)  
     #----------------------------------
     
     #Compute TB----------------------------------------------------------------------
     for side in self.mesh.side_list['Boundary']:
      elem = self.mesh.side_elem_map[side][0]
      normal = self.mesh.compute_side_normal(elem,side)
      if np.dot(self.dom['phonon_dir'][t][p],normal) > 0:
       value = self.B1[m]*symmetry*temp[elem]*np.dot(self.dom['S'][t][p],normal)/np.pi
       new_boundary_temp[elem] += value
      #------------------------------------------------------------------------------

      #-----------------------   
     #for n,i in enumerate(temp):
     # B2 = self.x[n]*self.B2[m] + (1.0-self.x[n])*self.B2[m]
     # new_temp += B2 * temp * self.dom['d_omega'][t][p]/4.0/np.pi * symmetry

    # new_temp += self.B2[m] * np.multiply(temp,self.x) * self.dom['d_omega'][t][p]/4.0/np.pi * symmetry
     new_temp += self.B2[m] * temp * self.dom['d_omega'][t][p]/4.0/np.pi * symmetry

     for i in range(self.n_el) :
      flux[i] += self.B1[m]*temp[i]*self.dom['S'][t][p]

     #NEW------------------------------------------------------------------------------
     for i,j in zip(*Fminus.nonzero()):
      a = self.get_transmission(i,j)
      suppression[m,t,p] +=  0.5*Fminus[i,j]*a*temp[i]*np.dot(self.dom['S'][t][p],self.mesh.get_af(i,j))/self.mfp[m]

     for i,j in zip(*Fplus.nonzero()):
      suppression[m,t,p] +=  0.5*Fplus[i,j]*temp[i]*np.dot(self.dom['S'][t][p],self.mesh.get_af(i,j))/self.mfp[m]
     #----------------------------------------------------------------------------------


   #Create output------
   suppression *= kappa_factor
   DS = csc_matrix( (np.array(dd),(np.array(rd),np.array(cd))),shape=(self.n_elems,self.n_elems))
   DS = np.array(DS.todense())
   kappa = np.array([kappa])
   output = {'kappa':kappa,'temp':new_temp,'DS':DS,'boundary_temp':new_boundary_temp,'suppression':suppression,'flux':flux}

   return output



  #def compute_gradient(self):
  # if MPI.COMM_WORLD.Get_rank() == 0:
  #  print('Gradient Calculation started....')
  # gradient = np.zeros(self.n_elems)
  # output = compute_sum(self.solve_gradient,self.n_phi,{'output',np.zeros(self.n_elems)},{})
  # return gradient
  #def solve_gradient(self):


   




  def compute_function(self,**argv):
 
   #fourier_temp = argv['fourier_temperature']
   fourier_temp = np.zeros(self.n_elems)
   max_iter = argv.setdefault('max_bte_iter',10)
   max_error = argv.setdefault('max_bte_error',1e-2)
   error = 2.0 * max_error
   previous_temp = fourier_temp
   previous_boundary_temp = fourier_temp
  
   previous_kappa = 0.0
   n = 0

   #Initialize DF to Fourier modeling---
   rd = [];cd = [];dd = []
   for i,j in zip(*self.B.nonzero()):
    rd.append(i);cd.append(j)
    dd.append(fourier_temp[i])
   DS = csc_matrix( (np.array(dd),(np.array(rd),np.array(cd))),shape=(self.n_elems,self.n_elems))
   DS = DS.todense()
   #---------------------------------
   #-----------------------------------
   if MPI.COMM_WORLD.Get_rank() == 0:
    print('    Iter    Thermal Conductivity [W/m/K]      Error')
    print('   ---------------------------------------------------')

   while error > max_error and n < max_iter:

    options = {'DS':np.array(DS),'temp':np.array(previous_temp),'boundary_temp':np.array(previous_boundary_temp)}
    output = {'DS':np.zeros((self.n_el,self.n_el)),'temp':np.zeros(self.n_el),'kappa':np.array([0],dtype=np.float64),'boundary_temp':np.zeros(self.n_el),'suppression':np.zeros((self.n_mfp,self.n_theta,self.n_phi)),'flux':np.zeros((self.n_el,3))}

    #solve---
    output = compute_sum(self.solve,self.n_phi,output,options)
    #----------------
    kappa = output['kappa']
    #print(kappa)
    #quit()
    previous_temp = output['temp']
    flux = output['flux']
    previous_boundary_temp = output['boundary_temp']
    directional_suppression = output['suppression']

    #Compute kappa---------------------------
    kappa = 0
    for m in range(self.n_mfp):
     for t in range(self.n_theta):
      for p in range(self.n_phi):
       kappa += directional_suppression[m,t,p]
    #----------------------------------------
 
    DS = output['DS']
    error = abs((kappa-previous_kappa))/kappa
    if MPI.COMM_WORLD.Get_rank() == 0:
     print('{0:7d} {1:20.4E} {2:25.4E}'.format(n, kappa, error))
    
    previous_kappa = kappa
    n +=1


   #Apply symmetry-----------------
   Nt = int(self.n_theta/2.0)
   for p in range(self.n_phi):
    for t in range(Nt):
     for m in range(self.n_mfp):
      directional_suppression[m][self.n_theta-t-1][p] = directional_suppression[m][t][p]
   #-----------------------------
   #Compute suppression function
   suppression = np.zeros(self.n_mfp) 
   for m in range(self.n_mfp):
    for t in range(self.n_theta):
     for p in range(self.n_phi):
      suppression[m] += directional_suppression[m,t,p]
 
   self.state = {'bte_kappa':kappa*self.kappa_bulk,\
            'directional_suppression':directional_suppression,\
            'suppression_function':suppression,\
            'dom':self.dom,\
            'mfp':self.mfp*1e-9,\
            'bte_temperature':previous_temp,\
            'bte_flux':flux}


   if MPI.COMM_WORLD.Get_rank() == 0:
    print('   ---------------------------------------------------')


