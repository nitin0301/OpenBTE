from __future__ import print_function
from __future__ import absolute_import
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
from .utils import *
import deepdish as dd
import sparse
import time
from termcolor import colored
from .WriteVTK import *
from .geometry import *
from .material import *

#from fourier import Fourier

class Solver(object):
  
  def __init__(self,**argv):

   self.mesh = Geometry(model='load',filename = argv.setdefault('geometry_filename','geometry'))

   #self.mesh = argv['geometry']
   self.n_elems = len(self.mesh.elems)
   self.dim = self.mesh.dim
   self.mfe = False
   self.n_mfp = 1
   self.mfp = np.array([1e-9])
   #self.x = argv.setdefault('density',np.ones(len(self.mesh.elems)))

   self.multiscale = argv.setdefault('multiscale',False)
   self.only_fourier = argv.setdefault('only_fourier',False)
   #Get material properties-----
  # mat = argv['material']
   mat = Material(model='load')
   #self.B0 = mat.state['B0']
   #self.B1 = mat.state['B1']
   #self.B2 = mat.state['B2']

   #MAT 1
   self.v_1 = 1e4
   self.c_1 = 1e6
   self.kappa_1 = 150.0
   self.tau_1 = 3.0*self.kappa_1/self.v_1/self.v_1/self.c_1

   #MAT 2
   self.v_2 = 1e4
   self.c_2 = 1e6
   self.kappa_2 = 150.0
   self.tau_2 = 3.0*self.kappa_2/self.v_2/self.v_2/self.c_2


   self.density = argv['density']
   self.material_gradient = argv['material_gradient']



   #self.kappa_bulk = mat.state['kappa_bulk_tot']

   self.lu = {}


   #INITIALIZATION-------------------------
   self.n_el = len(self.mesh.elems)
   #data = self.mesh.compute_boundary_condition_data()

   #self.side_periodic_value = data['side_periodic_value']
   #self.area_flux = data['area_flux']
   #self.kappa_factor = self.mesh.size[0]/self.area_flux
   self.kappa_factor = self.mesh.kappa_factor

   self.print_logo()  
   #quit() 

   self.dom = mat.state['dom']
   self.n_theta = self.dom['n_theta']
   self.n_phi = self.dom['n_phi']

   
   self.print_dof()

   #apply symmetry--------
   if self.dim == 2:
    self.n_theta_irr = int(self.n_theta/2)
    self.symmetry = 2.0
    self.n_index = self.n_phi
   else:
    self.n_theta_irr = 1
    self.symmetry = 1.0
    self.n_index = self.n_phi * self.n_theta
   #---------------------

   #if self.compute_matrix:
   if MPI.COMM_WORLD.Get_rank() == 0:
    directory = 'tmp'
    if os.path.exists(directory):
     shutil.rmtree(directory)
    os.makedirs(directory)
    #compute matrix--------
   if argv['max_bte_iter'] > 0:
    output = compute_sum(self.compute_directional_connections,self.n_index)

   #output = compute_sum(self.compute_directional_diffusion,self.n_phi*self.n_theta,{},{})

   self.assemble_fourier()  
   
   #quit() 
   #solve the BTE 
   self.compute_function(**argv)

   #if MPI.COMM_WORLD.Get_rank() == 0:
   # print('')
   # print('Solving BTE....done.')
   # print('')
    #print('Effective Thermal Conductivity (BTE):   ' + str(round(self.state['kappa_bte'],4)) + r' W/m/K')
    #print('')

   if MPI.COMM_WORLD.Get_rank() == 0:
    if os.path.isdir('tmp'):
     shutil.rmtree('tmp')


   #Write data-------  
   argv.update({'Geometry':self.mesh})
   vw = WriteVtk(argv)
   vw.add_variable(self.state['fourier_temperature'],label = 'Fourier Temperature [K]')
   vw.add_variable(self.state['fourier_flux'],label = r'''Thermal Flux [W/m/m]''')
   if argv['max_bte_iter'] > 0:
    vw.add_variable(self.state['bte_temperature'],label = r'''BTE Temperature [K]''')
    vw.add_variable(self.state['bte_flux'],label = r'''BTE Thermal Flux [W/m/m]''')
   vw.write_vtk()  
 
   #----------------

   #SAVE FILE--------------------
   if argv.setdefault('save',True):
    if MPI.COMM_WORLD.Get_rank() == 0:
     dd.io.save(argv.setdefault('filename','solver') + '.hdf5', self.state)


  def compute_directional_diffusion(self,index,options):

    t = int(index/self.n_phi)
    p = index%self.n_phi
    ss = self.dom['ss'][t][p]

    row_tmp = []
    col_tmp = []
    data_tmp = [] 
    for kc1,kc2 in zip(*self.mesh.A.nonzero()):
     ll = self.mesh.get_side_between_two_elements(kc1,kc2)  
     (v_orth,dummy) = self.mesh.get_decomposed_directions(kc1,kc2,rot=ss)
     row_tmp.append(kc1)
     col_tmp.append(kc2)
     #data_tmp.append(self.mesh.get_side_periodic_value(ll,kc2,self.side_periodic_value)*v_orth)
     data_tmp.append(self.mesh.get_side_periodic_value(ll,kc2)*v_orth)
    RHS = csc_matrix( (np.array(data_tmp),(np.array(row_tmp),np.array(col_tmp))), shape=(self.n_elems,self.n_elems) )

    scipy.io.mmwrite('tmp/RHS_DIFF_' + str(t) + '_' + str(p) + r'.mtx',RHS) 


  def get_kappa(self,kc1,kc2):

   kappa_1 = self.density[kc1]*self.kappa_1 + (1.0 - self.density[kc1])*self.kappa_2
   kappa_2 = self.density[kc2]*self.kappa_1 + (1.0 - self.density[kc2])*self.kappa_2
   
   return 2.0*kappa_1*kappa_2/(kappa_1+kappa_2)
  

  def get_tau(self,kc):
   tau = self.density[kc]*self.tau_1 + (1.0 - self.density[kc])*self.tau_2
   return tau 

  def get_v(self,kc):
   v = self.density[kc]*self.v_1 + (1.0 - self.density[kc])*self.v_2
   return v*1e9

  def get_C(self,kc):
   C = self.density[kc]*self.C_1 + (1.0 - self.density[kc])*self.C_2
   return C

  def compute_directional_connections(self,index,options):

    
   Diff = []
   Fminus = []
   Fplus = []
   r=[];c=[];d=[]
   rk=[]; ck=[]; dk=[]

   P = np.zeros(self.n_elems)

   if self.dim == 2:
    p = index
    angle_factor = self.dom['phi_dir'][index]/self.dom['d_phi_vec'][index]
   else:
    t = int(index/self.n_phi)
    p = index%self.n_phi
    #theta_factor = self.dom['at'][t] / self.dom['d_theta_vec'][t]
    #phi_factor = self.dom['phi_dir'][p]/self.dom['d_phi_vec'][p]

    angle_factor = self.dom['S'][t][p]/self.dom['d_omega'][t][p]
    #angle_factor = phi_factor * theta_factor     
 
   for i,j in zip(*self.mesh.A.nonzero()):
   
    side = self.mesh.get_side_between_two_elements(i,j)  
    coeff = np.dot(angle_factor,self.mesh.get_coeff(i,j))

    if coeff > 0:
     r.append(i); c.append(i); d.append(coeff*self.get_v(i)*self.get_tau(i))
     v = self.mesh.get_side_periodic_value(side,i)
     rk.append(i); ck.append(j);      
     dk.append(v*coeff*self.mesh.get_elem_volume(i)/self.get_v(i)/self.get_tau(i))

    if coeff < 0 :
     r.append(i); c.append(j); d.append(coeff*self.get_v(j)*self.get_tau(j))
     v = self.mesh.get_side_periodic_value(side,j)
     P[i] -= v*coeff
     rk.append(i); ck.append(j);      
     dk.append(-v*coeff*self.mesh.get_elem_volume(i)/self.get_v(j)/self.get_tau(j))

   #Write the boundaries------
   HW_minus = np.zeros(self.n_elems)
   HW_plus = np.zeros(self.n_elems)
   for side in self.mesh.side_list['Boundary']:
    (coeff,elem) = self.mesh.get_side_coeff(side)
    tmp = np.dot(coeff,angle_factor)
    if tmp > 0:
     r.append(elem); c.append(elem); d.append(tmp)
     if self.dim == 2:
      HW_plus[elem] = np.dot(self.dom['phi_dir'][p],self.mesh.get_side_normal(0,side))/np.pi
     else:
      HW_plus[elem] = np.dot(self.dom['S'][t][p],self.mesh.get_side_normal(0,side))/np.pi
    else :
     HW_minus[elem] = -tmp

   #-------------
   
   #Thermalize boundaries--------------------------
   Hot = np.zeros(self.n_elems)
   for side in self.mesh.side_list['Hot']:
    (coeff,elem) = self.mesh.get_side_coeff(side)
    tmp = np.dot(coeff,angle_factor)
    if tmp < 0:
     Hot[elem] = -tmp*0.5
    else:
     r.append(elem); c.append(elem); d.append(tmp)

   Cold = np.zeros(self.n_elems)
   #Kdiff_minus = np.zeros(self.n_elems)
   #Kdiff_plus = np.zeros(self.n_elems)
   for side in self.mesh.side_list['Cold']:
    (coeff,elem) = self.mesh.get_side_coeff(side)
    tmp = np.dot(coeff,angle_factor)
    if tmp < 0:
     Cold[elem] = tmp*0.5
    else:
     r.append(elem); c.append(elem); d.append(tmp)
   #  Kdiff_plus[elem] = np.dot(coeff,angle_factor)*self.mesh.get_elem_volume(elem)

   #-----------------------------------------------

   #--------------------------WRITE FILES---------------------
   A = csc_matrix( (d,(r,c)), shape=(self.n_elems,self.n_elems) )
   K = csc_matrix( (dk,(rk,ck)), shape=(self.n_elems,self.n_elems) )
   scipy.io.mmwrite('tmp/A_' + str(index) + r'.mtx',A) 
   scipy.io.mmwrite('tmp/K_' + str(index) + r'.mtx',K) 
   P.dump(file('tmp/P_' + str(index) +r'.np','wb+'))
   HW_minus.dump(file('tmp/HW_MINUS_' + str(index) +r'.np','w+'))
   HW_plus.dump(file('tmp/HW_PLUS_' + str(index) +r'.np','w+'))
   Hot.dump(file('tmp/Hot_' + str(index) +r'.np','w+'))
   Cold.dump(file('tmp/Cold_' + str(index) +r'.np','w+'))
   #Kdiff_minus.dump(file('tmp/Kdiff_minus_' + str(index) +r'.np','w+'))
   #Kdiff_plus.dump(file('tmp/Kdiff_plus_' + str(index) +r'.np','w+'))

  def solve_bte(self,index,options):

   #if self.current_iter == 0:
   A = scipy.io.mmread('tmp/A_' + str(index) + '.mtx').tocsc()
   P = np.load(file('tmp/P_' + str(index) +r'.np','rb'))
   HW_MINUS = np.load(file('tmp/HW_MINUS_' + str(index) +r'.np','r'))
   HW_PLUS = np.load(file('tmp/HW_PLUS_' + str(index) +r'.np','r'))
   K = scipy.io.mmread('tmp/K_' + str(index) + '.mtx').tocsc()
   Hot = np.load(file('tmp/Hot_' + str(index) +r'.np','r'))
   Cold = np.load(file('tmp/Cold_' + str(index) +r'.np','r'))
   #Kdiff_minus_ = np.load(file('tmp/Kdiff_minus_' + str(index) +r'.np','r'))
   #Kdiff_plus_ = np.load(file('tmp/Kdiff_plus_' + str(index) +r'.np','r'))

   TB = options['boundary_temperature']
   suppression_fourier = options['suppression_fourier']
   TL = options['lattice_temperature']
   #print(max(TL))
   temp_fourier = options['temperature_fourier']
   temp_fourier_gradient = options['temperature_fourier_gradient']
   TL_new = np.zeros(self.n_el) 
   TB_new = np.zeros(self.n_el) 
   #phi_factor = self.dom['phi_dir'][p]/self.dom['d_phi_vec'][p]
   D = np.multiply(TB,HW_MINUS)

   flux = np.zeros((self.n_el,3)) 
   suppression = np.zeros((self.n_mfp,self.n_theta,self.n_phi))
   temperature_mfp = np.zeros((self.n_mfp,self.n_elems))
   ff_1 = 0
   ff_0 = 0
   
   #for t in range(int(self.n_theta/2.0)): #We take into account the symmetry
   for tt in range(self.n_theta_irr): #We take into account the symmetry
    
    if self.dim == 2:
     t = tt
     p = index
     theta_factor = self.dom['at'][t] / self.dom['d_theta_vec'][t]
    else:
     t = int(index/self.n_phi)
     p = index%self.n_phi
     theta_factor = 1.0
    
    pre_factor = 3.0/4.0/np.pi*0.5*theta_factor * self.dom['d_omega'][t][p]#  self.dom['d_theta_vec'][t]*self.dom['d_phi_vec'][p]
    ss = self.dom['ss'][t][p]
    fourier = False
    zeroth_order = False
    #RHS_DIFF = scipy.io.mmread('tmp/RHS_DIFF_' + str(t) + '_' + str(p) + '.mtx').tocsc()
    #----------------------------------------------------------------------------------
    F = scipy.sparse.eye(self.n_elems) + theta_factor * A
    lu = splu(F.tocsc())
    RHS = theta_factor * (P + D) + TL
    temp = lu.solve(RHS)

    sup = pre_factor * K.dot(temp-TL).sum()*self.kappa_factor
   

    TL_new += temp * self.dom['d_omega'][t][p]/4.0/np.pi * self.symmetry
    flux += np.outer(temp,np.multiply(self.dom['S'][t][p],[2.0,2.0,0.0]))

    m = 0
    suppression[m,t,p] += sup
    temperature_mfp[m] += temp*self.dom['d_omega'][t][p]*self.symmetry/4.0/np.pi
    suppression[m,self.n_theta -t -1,p] += suppression[m,t,p]
    TB_new += self.dom['at'][t]*np.multiply(temp,HW_PLUS)*self.symmetry

     #------------------------------------------------------------------------------------
   #suppression *= kappa_factor
   output = {'boundary_temperature':TB_new,'suppression':suppression,'flux':flux,'temperature':temperature_mfp,'ms':np.array([float(ff_0),float(ff_1)])}
   #output = {}

   return output

  
  def  compute_diff(self,x,TL,mm):

   m = mm[0]
   t = mm[1]
   p = mm[2]
 
   sup = 0.0   
   for side in self.mesh.side_list['Cold']:
    (coeff,elem) = self.mesh.get_side_coeff(side)
    tmp = np.dot(coeff,self.dom['S'][t][p])*self.mesh.get_elem_volume(elem)
    
    sup += tmp*(x[elem]-0.5)/2.0
    #print(-0.5-x[elem])
    #if tmp < 0:
    # sup += tmp*0.5

    #if tmp > 0:
    # sup += tmp*(x[elem])
    
   return sup/self.mfp[m]



  def compute_function(self,**argv):
 
   max_iter = argv.setdefault('max_bte_iter',10)
   max_error = argv.setdefault('max_bte_error',1e-2)
   error = 2.0 * max_error
  
   previous_kappa = 0.0
   self.current_iter = 0

   #First Step Fourier----------------- 
   #if MPI.COMM_WORLD.Get_rank() == 0:
   # print('')
    #print('Solving Fourier... started.')
   # print colored('Solving Fourier... started', 'green')

   #if MPI.COMM_WORLD.Get_rank() == 0:
   #   print('  ')
   #   print('   Bulk Thermal Conductivity:    ' + str(round(self.kappa_bulk,4)) + ' W/m/K')
   #Solve standard Fourier---------------------------------------------------------


   #Initalization-----------------------------------------------------------
   suppression = np.zeros((self.n_mfp,self.n_theta,self.n_phi))
   suppression_fourier = np.zeros((self.n_mfp,self.n_theta,self.n_phi))
   temperature = np.zeros((self.n_mfp,self.n_el))
   temperature_fourier = np.zeros((self.n_mfp,self.n_el))
   temperature_gradient = np.zeros((self.n_mfp,self.n_el,3))
   flux_fourier = np.zeros((self.n_el,3))
   #suppression_first = np.zeros(self.n_mfp)
   ms = np.zeros(2)
   #-----------------------------------------------------------------------
   suppression_fourier = np.zeros((1,self.n_theta,self.n_phi))
   
   temperature_fourier = np.zeros((1,self.n_el))
   compute_sum(self.solve_fourier,1,output =  {'suppression':suppression_fourier,\
                                                        'temperature':temperature_fourier,\
                                                        'temperature_gradient':np.zeros((1,self.n_el,3)),\
                                                        'flux':flux_fourier},\
                                             options = {'kappa_bulk':[1.0],'mfe_factor':0.0})
   #---------------------------------------------------------------------
   #---------------------------------------------------------------------
   

   #kappa = suppression_fourier[0,:,:].sum() 
   kappa = suppression_fourier[0,:,:].sum() 

   kappa_fourier = kappa
   suppression = np.array(self.n_mfp * [suppression_fourier[0]])
   #tmp = suppression.copy()
   fourier_temp = temperature_fourier[0]
   suppression_fourier = np.zeros((self.n_mfp,self.n_theta,self.n_phi))
   temperature_fourier = np.zeros((self.n_mfp,self.n_el))
   #supression_zeroth = np.array([tmp[m,:,:].sum() for m in range(np.shape(tmp)[0])])
    
   suppression_fourier = np.zeros((self.n_mfp,self.n_theta,self.n_phi))

   if MPI.COMM_WORLD.Get_rank() == 0:
      print('  ')
      print('   Fourier Thermal Conductivity: ' + str(round(kappa,4)) + ' W/m/K')
      print('  ')

   
   #if MPI.COMM_WORLD.Get_rank() == 0:
   # print colored('Solving Fourier... done', 'green')
   # print('')

   #suppression_fourier = self.n_mfp * [output_fourier['suppression'][0]]
   #temperature_fourier= self.n_mfp * [output_fourier['temperature'][0]]
   #temperature_fourier_gradient = self.n_mfp * [output_fourier['temperature_gradient'][0]]

   #fourier_temp = output_fourier['temperature'][0]

   #fourier_temp = sum([self.B2[m]*temperature_fourier[m,:] for m in range(self.n_mfp)])   
   lattice_temperature = fourier_temp.copy()
   #print(min(lattice_temperature),max(lattice_temperature))
   boundary_temperature = np.multiply(self.boundary_mask,fourier_temp)
   #------------------------------------ 

   

   #-----------------------------------
   if MPI.COMM_WORLD.Get_rank() == 0:
    if max_iter > 0:
     print('')
     print(colored('Solving BTE... started', 'green'))
     print('')
     print('    Iter    Thermal Conductivity [W/m/K]      Error       Zeroth - Fourier - BTE')
     print('   ------------------------------------------------------------------------------------')



   flux = np.zeros((self.n_elems,3))
   kappa = 0.0
   suppression_zeroth = np.zeros(self.n_mfp)
   while error > max_error and self.current_iter < max_iter:

    #zeroth order---------------------------
    suppression_zeroth = np.array([self.compute_diffusive_thermal_conductivity(lattice_temperature,mat = np.eye(3))])


    if self.multiscale:

       compute_sum(self.solve_fourier,self.n_mfp,output =  {'suppression':suppression_fourier,\
                                                            'temperature':temperature_fourier,\
                                                            'temperature_gradient':temperature_gradient,\
                                                            'flux':flux},\
                                                 options = {'boundary_temperature':boundary_temperature.copy(),\
                                                            'lattice_temperature':lattice_temperature.copy(),\
                                                            'interface_conductance': 3.0/2.0/self.mfp,\
                                                            'kappa_bulk':np.square(self.mfp)/3.0,\
                                                            'mfe_factor':1.0})

        
       #if self.only_fourier:
       # lattice_temperature = sum([self.B2[m]*temperature_fourier[m,:] for m in range(self.n_mfp)])   
       # boundary_temperature = np.multiply(self.boundary_mask,lattice_temperature)
       # suppression = suppression_fourier.copy()
       # sup = [suppression[m,:,:].sum() for m in range(np.shape(suppression)[0])]

        #if MPI.COMM_WORLD.Get_rank() == 0:
        # S0 = 0.0
        # for m in range(self.n_mfp):
        #  S0 += self.compute_diffusive_thermal_conductivity(temperature_fourier[m],G_R=3.0/2.0/self.mfp[m])*self.B2[m]
         #S0_2 = self.compute_diffusive_thermal_conductivity(lattice_temperature,mat = np.eye(3))
         #print(S0_2,S0)
         
         #print(np.dot(self.B2,sup))

        #S0 = np.dot(sup,self.B2)

    
    if not self.only_fourier:
     #BTE-------------------------------------------------------------------------------

     #suppression = np.zeros((self.n_mfp,self.n_theta,self.n_phi))
     #temperature = np.zeros((self.n_mfp,self.n_elems))
     #boundary_temperature = np.zeros(self.n_elems)
     #old_TL = lattice_temperature.copy()
     #old_TB = boundary_temperature.copy()

     
     compute_sum(self.solve_bte,self.n_index, \
                          output = {'temperature':temperature,\
                                    'flux':flux,\
                                    'suppression':suppression,\
                                    'ms':ms,\
                                    'boundary_temperature':boundary_temperature},\
                          options = {'lattice_temperature':lattice_temperature.copy(),\
                                     'suppression_fourier':suppression_fourier.copy(),\
                                     'boundary_temperature':boundary_temperature.copy(),\
                                     'temperature_fourier':temperature_fourier.copy(),\
                                     'temperature_fourier_gradient':temperature_gradient.copy(),\
                                     'suppression_fourier':suppression_fourier.copy(),\
                                     'suppression_zeroth':suppression_zeroth.copy()})
     #compute_sum(self.solve_bte,self.n_index, \
     #                     output = {},\
     #                     options = {'lattice_temperature':lattice_temperature.copy(),\
     #                                'suppression_fourier':suppression_fourier.copy(),\
     #                                'boundary_temperature':boundary_temperature.copy(),\
     #                                'temperature_fourier':temperature_fourier.copy(),\
     #                                'temperature_fourier_gradient':temperature_gradient.copy(),\
      #                               'suppression_fourier':suppression_fourier.copy(),\
     #                                'suppression_zeroth':suppression_zeroth.copy()})


     #--------------------------------------------------------------------------------
     lattice_temperature = temperature[0]#np.sum([self.B2[m]*temperature[m,:] for m in range(self.n_mfp)],axis=0)

     #lattice_temperature = 0.5*lattice_temperature + 0.5 * old_TL
     #boundary_temperature = 0.5*boundary_temperature + 0.5 * old_TB
    #--------------------------

    print(max(lattice_temperature))
    #kappa = sum([self.B0[m]*suppression[m,:,:].sum() for m in range(self.n_mfp)])
    kappa = sum([suppression[0,:,:].sum() for m in range(self.n_mfp)])
    error = abs((kappa-previous_kappa))/kappa
   
    if MPI.COMM_WORLD.Get_rank() == 0:
     #print(kappa*self.kappa_bulk)
     ms_0 = ms[0]/float(self.n_mfp)/float(self.n_theta)/float(self.n_phi)*self.symmetry
     ms_1 = ms[1]/float(self.n_mfp)/float(self.n_theta)/float(self.n_phi)*self.symmetry
     #if max_iter > 0:
     print('{0:7d} {1:20.4E} {2:25.4E} {3:10.2E} {4:8.1E} {5:8.1E}'.format(self.current_iter,kappa, error,ms_0,ms_1,1.0-ms_0-ms_1))

    previous_kappa = kappa
    self.current_iter +=1
    #------------------------

    #lattice_temperature = output['temperature']
    #flux = output['flux']
    #if MPI.COMM_WORLD.Get_rank() == 0:
     #print(min(lattice_temperature),max(lattice_temperature))
    #-----------------------
  
   
 
   #suppression = [directional_suppression[m,:,:].sum() for m in range(self.n_mfp)]

   #Compute zero suppression---------
   #zero_suppression = self.n_mfp * [self.compute_diffusive_thermal_conductivity(previous_temp)]
   #------------------------------
   #Iso suppression---------
   #iso_suppression = [self.compute_diffusive_thermal_conductivity(output['temperature_mfp'][m]) for m in range(self.n_mfp)]


       #if self.only_fourier:
       # lattice_temperature = sum([self.B2[m]*temperature_fourier[m,:] for m in range(self.n_mfp)])   

   #if MPI.COMM_WORLD.Get_rank() == 0:
   # print(len(old_TB.nonzero()[0]))


   #compute_sum(self.solve_fourier,self.n_mfp,output =  {'suppression':suppression_fourier,\
   #                                                         'temperature':temperature_fourier,\
   #                                                         'temperature_gradient':temperature_gradient,\
   #                                                         'flux':flux},\
   #                                              options = {'boundary_temperature':old_TB.copy(),\
   #                                                         'lattice_temperature':old_TL.copy(),\
   #                                                         'interface_conductance': 3.0/2.0/self.mfp,\
   #                                                         #'interface_conductance': [0.0] * self.n_mfp,\
   #                                                         'kappa_bulk':np.square(self.mfp)/3.0,\
   #                                                         'mfe_factor':1.0})




   self.state = {'kappa_bte':kappa,\
            'suppression':suppression,\
            #'suppression_function':suppression,\
            'zero_suppression':suppression_zeroth,\
            #'iso_suppression':iso_suppression,\
            'fourier_suppression':suppression_fourier,\
            'dom':self.dom,\
            'kappa_fourier':kappa,\
            'mfp':self.mfp*1e-9,\
            'bte_temperature':lattice_temperature,\
            'fourier_temperature':fourier_temp,\
            'fourier_flux':flux_fourier,\
            'bte_flux':flux}

   if MPI.COMM_WORLD.Get_rank() == 0:
    if max_iter > 0 :
     print('   ------------------------------------------------------------------------------------')

   if max_iter > 0:
    if MPI.COMM_WORLD.Get_rank() == 0:
     #print('')
     #print('Solving BTE... done.')
     print('')
     print(colored('Solving BTE... done', 'green'))
     print('')


  def assemble_fourier(self) :
   
   if  MPI.COMM_WORLD.Get_rank() == 0:
    row_tmp = []
    col_tmp = []
    data_tmp = [] 
    row_tmp_b = []
    col_tmp_b = []
    data_tmp_b = [] 
    data_tmp_b2 = [] 
    B = np.zeros(self.n_elems)
    for kc1,kc2 in zip(*self.mesh.A.nonzero()):
     kappa = self.get_kappa(kc1,kc2)
     ll = self.mesh.get_side_between_two_elements(kc1,kc2)  
     (v_orth,dummy) = self.mesh.get_decomposed_directions(kc1,kc2)
     vol1 = self.mesh.get_elem_volume(kc1)
     vol2 = self.mesh.get_elem_volume(kc2)
     row_tmp.append(kc1)
     col_tmp.append(kc2)
     data_tmp.append(-v_orth/vol1*kappa)
     row_tmp.append(kc1)
     col_tmp.append(kc1)
     data_tmp.append(v_orth/vol1*kappa)
     #----------------------
     row_tmp_b.append(kc1)
     col_tmp_b.append(kc2)
     data_tmp_b.append(self.mesh.get_side_periodic_value(ll,kc2)*v_orth*kappa)
     data_tmp_b2.append(self.mesh.get_side_periodic_value(ll,kc2))
     #---------------------
     B[kc1] += self.mesh.get_side_periodic_value(ll,kc2)*v_orth/vol1*kappa
     
    #Boundary_elements
    FF = np.zeros(self.n_elems)
    boundary_mask = np.zeros(self.n_elems)
    for side in self.mesh.side_list['Boundary']:
     elem = self.mesh.side_elem_map[side][0]
     vol = self.mesh.get_elem_volume(elem)
     area = self.mesh.get_side_area(side)
     FF[elem] = area/vol
     boundary_mask[elem] = 1.0

 
    F = csc_matrix((np.array(data_tmp),(np.array(row_tmp),np.array(col_tmp))), shape=(self.n_elems,self.n_elems) )
    RHS = csc_matrix( (np.array(data_tmp_b),(np.array(row_tmp_b),np.array(col_tmp_b))), shape=(self.n_elems,self.n_elems) )
    PER = csc_matrix( (np.array(data_tmp_b2),(np.array(row_tmp_b),np.array(col_tmp_b))), shape=(self.n_elems,self.n_elems) )

    #-------    
    data = {'F':F,'RHS':RHS,'B':B,'PER':PER,'boundary_mask':boundary_mask,'FF':FF}
   else: data = None
   data =  MPI.COMM_WORLD.bcast(data,root=0)
   self.F = data['F']
   self.RHS = data['RHS']
   self.PER = data['PER']
   self.B = data['B']
   self.FF = data['FF']
   #self.Thermo = data['Thermo']
   #self.T_fixed = data['T_fixed']
   #self.FF_Thermo = data['FF_Thermo']
   self.boundary_mask = data['boundary_mask']


  def solve_fourier(self,n,options):

    #Update matrices-------
    kappa_bulk = options['kappa_bulk'][n]
    #self.mfe = options.setdefault('mfe',False)
    G = options.setdefault('interface_conductance',np.zeros(self.n_mfp))[n]
    TL = options.setdefault('lattice_temperature',np.zeros(self.n_el))
    TB = options.setdefault('boundary_temperature',np.zeros(self.n_el))
    mfe_factor = options.setdefault('mfe_factor',0.0)
    #G = 0.0

    #G = 0.0

    diag_1 = diags([self.FF],[0]).tocsc()
    A = (self.F + diag_1*G)   + mfe_factor * csc_matrix(scipy.sparse.eye(self.n_elems))/kappa_bulk
    B = (self.B + np.multiply(self.FF,TB)*G)  + mfe_factor * TL/kappa_bulk
    if mfe_factor == 0.0:
     A[10,10] = 1.0
    #----------------------------------------------------

    #------------------------
    SU = splu(A)
    C = np.zeros(self.n_el)
    #--------------------------------------
    min_err = 1e-3
    error = 2*min_err
    max_iter = 10
    n_iter = 0
    kappa_old = 0

    #if options['verbose'] > 0:    
     
    
    n_kappa = len(options['kappa_bulk'])
    temperature_mfp = np.zeros((n_kappa,self.n_elems)) 
    gradient_temperature_mfp = np.zeros((len(options['kappa_bulk']),self.n_elems,3)) 
    while error > min_err and n_iter < max_iter :

     RHS = B + C#*kappa_bulk
     #if not 'temperature' in options.keys() and len(self.mesh.side_list['Hot']) == 0:
     if mfe_factor == 0.0:
       RHS[10] = 0.0
     temp = SU.solve(RHS)
     #print(min(temp),max(temp))
     temp = temp - (max(temp)+min(temp))/2.0
   
     (C,flux) = self.compute_non_orth_contribution(temp)

     kappa = self.compute_diffusive_thermal_conductivity(temp)
     print(kappa)

     error = abs((kappa - kappa_old)/kappa)
     kappa_old = kappa
     #if options['verbose'] > 0:    
     # if max_iter > 0:
     #  print('{0:7d} {1:20.4E} {2:25.4E}'.format(n_iter,kappa*self.kappa_bulk, error))
     n_iter +=1 
    #if options['verbose'] > 0:    
    # print('   ---------------------------------------------------')
    # print('  ')
    suppression = np.zeros(len(options['kappa_bulk']))
    #suppression[n] = kappa
    temperature_mfp[n] = temp

    gradient_temperature_mfp[n] = self.mesh.compute_grad(temp)
    gradient_temperature = self.mesh.compute_grad(temp)
    suppression = np.zeros((len(options['kappa_bulk']),self.n_theta,self.n_phi))
    for t in range(self.n_theta):
     for p in range(self.n_phi):
      suppression[n,t,p] = kappa*self.dom['ss'][t][p][self.mesh.direction][self.mesh.direction]*3.0/4.0/np.pi

    output = {'suppression':suppression,'temperature':temperature_mfp,'flux':flux,'temperature_gradient':gradient_temperature_mfp}

    return output

  def compute_non_orth_contribution(self,temp) :

    #self.gradT = self.mesh.compute_grad(temp,self.side_periodic_value)
    self.gradT = self.mesh.compute_grad(temp)
    C = np.zeros(self.n_el)
    for i,j in zip(*self.mesh.A.nonzero()):
     #Get agerage gradient----
     side = self.mesh.get_side_between_two_elements(i,j)
     w = self.mesh.get_interpolation_weigths(side)
     grad_ave = w*self.gradT[i] + (1.0-w)*self.gradT[j]
     #------------------------
     (dumm,v_non_orth) = self.mesh.get_decomposed_directions(i,j)
     
     C[i] += np.dot(grad_ave,v_non_orth)/2.0/self.mesh.get_elem_volume(i)*self.get_kappa(i,j)
     C[j] -= np.dot(grad_ave,v_non_orth)/2.0/self.mesh.get_elem_volume(j)*self.get_kappa(i,j)

    return C,-self.gradT


  #def compute_diffusive_thermal_conductivity(self,temp):

  # kappa = 0
  # for i,j in zip(*self.RHS.nonzero()):
  #  kappa += 0.5*self.RHS[i,j]*(temp[j]+self.RHS[i,j]/abs(self.RHS[i,j])-temp[i])*self.mesh.size[0]/self.area_flux

  # return kappa


  #def compute_diffusive_thermal_conductivity(self,temp,mat=np.eye(3),TL = 0.0,G_R = 1e20):
  def compute_diffusive_thermal_conductivity(self,temp,mat=np.eye(3)):

   kappa = 0
   for i,j in zip(*self.PER.nonzero()):
    (v_orth,dummy) = self.mesh.get_decomposed_directions(i,j,rot=mat)
    kappa += 0.5*v_orth *self.PER[i,j]*(temp[j]+self.PER[i,j]-temp[i])*self.get_kappa(i,j)

   #grad = self.mesh.compute_grad(temp)
   #kappa_2 = 0
   #for side in self.mesh.side_list['Cold']:
   # elem = self.mesh.side_elem_map[side][0]
   # area = self.mesh.compute_side_area(side)
    #if not self.mfe:
   # G_P = self.mesh.get_side_orthognal_direction(side)/area
    # G = pow(pow(G_P,-1) + pow(G_R,-1),-1)
    #else:
   # if G_R < 1e15:
   #   G = G_R/(1.0+G_R/G_P)
   # else:
   #   G = G_P


   # kappa_2 -= area*G*(-0.5 -temp[elem])
    #else :
    # normal = self.mesh.get_side_normal(0,side)    
    # kappa_2 -= np.dot(grad[elem],normal)
    # print(grad[elem])
     #kappa_2 = -factor*(-0.5-temp[elem])
 
   #kappa_3 = 0
   #for side in self.mesh.side_list['Hot']:
   # elem = self.mesh.side_elem_map[side][0]
   # v_orth = self.mesh.get_side_orthognal_direction(side)
   # kappa_3 -= v_orth*(0.5-temp[elem])


 
   return kappa*self.kappa_factor

  def print_logo(self):

   if MPI.COMM_WORLD.Get_rank() == 0:
    print(' ')
    print(r'''  ___                   ____ _____ _____ ''' )
    print(r''' / _ \ _ __   ___ _ __ | __ )_   _| ____|''')
    print(r'''| | | | '_ \ / _ \ '_ \|  _ \ | | |  _|  ''')
    print(r'''| |_| | |_) |  __/ | | | |_) || | | |___ ''')
    print(r''' \___/| .__/ \___|_| |_|____/ |_| |_____|''')
    print(r'''      |_|                                ''')
    print('')
    print('Giuseppe Romano [romanog@mit.edu]')
    print(' ')

  def print_dof(self):

   if MPI.COMM_WORLD.Get_rank() == 0:
    print(' ')
    print('Elements:          ' + str(self.n_el))
    print('Azimuthal angles:  ' + str(self.n_theta))
    print('Polar angles:      ' + str(self.n_phi))
    print('Mean-free-paths:   ' + str(1))
    print('Degree-of-freedom: ' + str(self.n_theta*self.n_phi*self.n_el))





