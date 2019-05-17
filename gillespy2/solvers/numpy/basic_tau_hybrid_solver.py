import random
from scipy.integrate import ode
import numpy
import math
import sys
import warnings
from itertools import combinations
import gillespy2
from gillespy2.core import GillesPySolver


eval_globals = math.__dict__


class BasicTauHybridSolver(GillesPySolver):
    """
    This Solver uses an algorithm that combines the Tau-Leaping and Hybrid ODE/Stochastic methods.
    A root-finding integration is considered over all reaction channels and continuous species rate
    rules, allowing both continuous and discrete regimes to be considered without partitioning the
    system.  Multiple reactions are fired in a single tau step, and the relative change in propensities
    is bounded by bounding the relative change in the state of the system, resulting in increased
    run-time performance with little accuracy trade-off.
    """
    name = "Basic Tau Hybrid Solver"

    def __init__(self, debug=False):
        self.debug = debug
        self.epsilon = 0.03
           
        
    def toggle_reactions(self, model, all_compiled, deterministic_reactions, dependencies, curr_state, rxn_offset, det_spec):
        
        #initialize variables
        inactive_reactions = all_compiled['inactive_rxns']
        rate_rules = all_compiled['rules']
        rxns = all_compiled['rxns']
        
        #Check if this reaction set is already compiled and in use:
        if deterministic_reactions in rate_rules.keys():
            return

        #If the set has changed, reactivate non-determinsitic reactions
        reactivate = []
        for r in inactive_reactions:
            if not r in deterministic_reactions:
                reactivate.append(r)
        for r in reactivate:
            rxns[r] = inactive_reactions.pop(r, None)

        # floor non-det species
        for s, d in det_spec.items():
            if not d and isinstance(curr_state[s], float):
                curr_state[s] = math.floor(curr_state[s])
            
        #Deactivate Determinsitic Reactions
        for r in deterministic_reactions:
            if not r in inactive_reactions:
                inactive_reactions[r] = rxns.pop(r, None)

        #Otherwise, this is a new determinstic reaction set that must be compiled
        if not deterministic_reactions in rate_rules:
            rate_rules[deterministic_reactions] = self.create_diff_eqs(deterministic_reactions, model, dependencies)
                
    def create_diff_eqs(self, comb, model, dependencies):

        diff_eqs = {}
        reactions = {}
        rate_rules = {}

        #Initialize sample dict
        for reaction in comb:
            for dep in dependencies[reaction]:
                if dep not in diff_eqs and model.listOfSpecies[dep].mode == 'dynamic':
                    diff_eqs[dep] = '0'

        # loop through each det reaction and concatenate it's diff eq for each species
        for reaction in comb:
            factor = {}
            for dep in dependencies[reaction]:
                factor[dep] = 0
            for key, value in model.listOfReactions[reaction].reactants.items():
                factor[key.name] -= value
            for key, value in model.listOfReactions[reaction].products.items():
                factor[key.name] += value
            for dep in dependencies[reaction]:
                if factor[dep] != 0:
                    diff_eqs[dep] += ' + {0}*({1})'.format(factor[dep], model.listOfReactions[reaction].propensity_function)
        
        #create a dictionary of compiled gillespy2 rate rules
        for spec, rate in diff_eqs.items():
            rate_rules[spec] = compile(gillespy2.RateRule(model.listOfSpecies[spec], rate).expression, '<string>', 'eval')

        # be sure to include model rate rules
        for i, rr in enumerate(model.listOfRateRules):
            rate_rules[rr] = compile(model.listOfRateRules[rr].expression, '<string>', 'eval')

        return rate_rules

    def flag_det_reactions(self, model, det_spec, det_rxn, dependencies):
        #Determine if each rxn would be deterministic apart from other reactions
        prev_state = det_rxn.copy()
        for rxn in model.listOfReactions:
            det_rxn[rxn] = True
            for species in dependencies[rxn]:
                if det_spec[species] == False:
                    det_rxn[rxn] = False
                    break
                    
        deterministic_reactions = set()
        for rxn in det_rxn:
            if det_rxn[rxn]:
                deterministic_reactions.add(rxn)
        deterministic_reactions = frozenset(deterministic_reactions)
        return deterministic_reactions
                            
    def calculate_statistics(self, model, propensities, curr_state, tau_step, det_spec, dependencies):
        """
        Calculates Mean, Standard Deviation, and Coefficient of Variance for each
        dynamic species, then set if species can be represented determistically
        """
        sd = {}
        CV = {}        

        mn = {species:curr_state[species] for (species, value) in 
              model.listOfSpecies.items() if value.mode == 'dynamic'}

        for r in model.listOfReactions:
                for reactant in model.listOfReactions[r].reactants:
                    if reactant.mode == 'dynamic':
                        mn[reactant.name] -= (tau_step * propensities[r])
                for product in model.listOfReactions[r].products:
                    if product.mode == 'dynamic':
                        mn[product.name] += (tau_step * propensities[r])
                
        # Get mean, standard deviation, and coefficient of variance for each dynamic species
        for species in mn:
            if mn[species] > 0:
                sd[species] = math.sqrt(mn[species])
                CV[species] = sd[species] / mn[species]
            else:
                sd[species], CV[species] = (0, 1)    # values chosen to guarantee discrete
            #Set species to deterministic if CV is less than threshhold
            det_spec[species] = True if CV[species] < self.epsilon else False                            
                
        return mn, sd, CV
    
    @staticmethod
    def __f(t, y, curr_state, reactions, rate_rules, propensities, compiled_reactions, compiled_rate_rules):
        """
        Evaluate the propensities for the reactions and the RHS of the Reactions and RateRules.
        """
        curr_state['t'] = t
        state_change = []

        for i, rr in enumerate(compiled_rate_rules):
            state_change.append(eval(compiled_rate_rules[rr], eval_globals, curr_state))
        for i, r in enumerate(compiled_reactions):
            propensities[r] = eval(compiled_reactions[r], eval_globals, curr_state)
            state_change.append(propensities[r])

        return state_change

    @staticmethod
    def __get_reaction_integrate(step, curr_state, y0, model, curr_time, propensities, compiled_reactions,
                                 compiled_rate_rules):
        """ Helper function to perform the ODE integration of one step """
        rhs = ode(BasicTauHybridSolver.__f)  # set function as ODE object
        rhs.set_initial_value(y0, curr_time).set_f_params(curr_state, model.listOfReactions,
                                                          model.listOfRateRules, propensities, compiled_reactions,
                                                          compiled_rate_rules)
        current = rhs.integrate(step + curr_time)  # current holds integration from current_time to int_time\
        if rhs.successful():
            return current, curr_time + step
        else:
            # if step is < 1e-15, take a Forward-Euler step for all species ('propensites' and RateRules)
            # TODO The RateRule linked species should still contain the correct value in current, verify this
            # step size is too small, take a single forward-euler step
            print('*** EULER ***')
            current = y0 + numpy.array(BasicTauHybridSolver.__f(curr_time, y0,
                                                                curr_state, model.listOfReactions,
                                                                model.listOfRateRules, propensities, compiled_reactions,
                                                                compiled_rate_rules)) * step

            return current, curr_time + step

    def __get_reactions(self, step, curr_state, y0, model, curr_time, save_time,
                        propensities, compiled_reactions, compiled_rate_rules, rxn_offset, debug):
        """
        Function to get reactions fired from t to t+tau.  This function solves for root crossings
        of each reaction channel from over tau step, using poisson random number generation
        to calculate distance to the root.  Returns four values:
        rxn_count - dict with key=Raection channel value=number of times fired
        current - list containing current displacement of each reaction channel for calculating fired reactions.
        curr_state - dict containing all state variables for system at current time
        curr_time - float representing current time
        """

        if curr_time + step > save_time:
            if debug:
                print("Step exceeds save_time, changing step size from ", step,
                      " to ", save_time - curr_time)
            step = save_time - curr_time

        if debug:
            print("Curr Time: ", curr_time, " Save time: ", save_time, "step: ", step)

        current, curr_time = self.__get_reaction_integrate(step, curr_state, y0, model,
                                                           curr_time, propensities, compiled_reactions,
                                                           compiled_rate_rules)

        # UPDATE THE STATE of the continuous species
        for i, s in enumerate(compiled_rate_rules):
            if curr_state[s] == current[i]:
                print('same continous state detected at ', curr_time)
            curr_state[s] = current[i]
            
        # UPDATE THE STATE of the discrete reactions
        for i, r in enumerate(compiled_reactions):
            rxn_offset[r] = current[i+len(compiled_rate_rules)]
        rxn_count = {}
        fired = False
        for i, r in enumerate(compiled_reactions):
            rxn_count[r] = 0
            while rxn_offset[r] > 0:
                if not fired:
                    fired = True
                rxn_count[r] += 1
                urn = (math.log(random.uniform(0, 1)))
                current[i+len(compiled_rate_rules)] += urn
                rxn_offset[r] += urn

        

        if debug:
            print("Reactions Fired: ", rxn_count)
            print("y(t) = ", current)
            print('rxn offset = ', rxn_offset)

        return rxn_count, current, curr_state, curr_time

    @classmethod
    def run(self, model, t=20, number_of_trajectories=1, increment=0.05, seed=None, debug=False,
            profile=False, show_labels=True, stochkit_home=None, **kwargs):
        """
        Function calling simulation of the model. This is typically called by the run function in GillesPy2 model
        objects and will inherit those parameters which are passed with the model as the arguments this run function.

        Attributes
        ----------

        model : GillesPy2.Model
            GillesPy2 model object to simulate
        t : int
            Simulation run time
        number_of_trajectories : int
            The number of times to sample the chemical master equation. Each
            trajectory will be returned at the end of the simulation.
            Optional, defaults to 1.
        increment : float
            Save point increment for recording data
        seed : int
            The random seed for the simulation. Optional, defaults to None.
        debug : bool (False)
            Set to True to provide additional debug information about the
            simulation.
        profile : bool (Fasle)
            Set to True to provide information about step size (tau) taken at each step.
        show_labels : bool (True)
            Use names of species as index of result object rather than position numbers.
        stochkit_home : str
            Path to stochkit. This is set automatically upon installation, but
            may be overwritten if desired.
        """
        if not sys.warnoptions:
            warnings.simplefilter("ignore")
        if not isinstance(self, BasicTauHybridSolver):
            self = BasicTauHybridSolver()
        if debug:
            print("t = ", t)
            print("increment = ", increment)

        if show_labels:
            trajectories = []
        else:
            num_save_points = int(t / increment) + 1
            trajectories = numpy.empty((number_of_trajectories, num_save_points, len(model.listOfSpecies)+1))

        det_spec = {species:True for (species, value) in model.listOfSpecies.items() if value.mode == 'dynamic'}
        det_rxn = {rxn:False for (rxn, value) in model.listOfReactions.items()}
        
        dependencies = {}


        for reaction in model.listOfReactions:
            dependencies[reaction] = set()
            [dependencies[reaction].add(reactant.name) for reactant in model.listOfReactions[reaction].reactants]
            [dependencies[reaction].add(product.name) for product in model.listOfReactions[reaction].products]

        if debug:
            print('dependencies')
            print(dependencies)
            print('det_spec')
            print(det_spec)
            print('inactive_rate_rules')
            print(inactive_rate_rules)

        for trajectory in range(number_of_trajectories):

            random.seed(seed)
            steps_taken = []
            steps_rejected = 0

            y0 = [0] * (len(model.listOfReactions) + len(model.listOfRateRules))
            rxn_offset = {}
            propensities = {}
            curr_state = {}
            curr_time = 0
            curr_state['vol'] = model.volume
            save_time = 0
                                
            if show_labels:
                results = {'time': []}
            else:
                results = numpy.empty((number_of_trajectories, int(t / increment) + 1, len(model.listOfSpecies) + 1))

            for s in model.listOfSpecies:
                # initialize populations
                curr_state[s] = model.listOfSpecies[s].initial_value
                if show_labels: results[s] = []

            for p in model.listOfParameters:
                curr_state[p] = model.listOfParameters[p].value

            for i, r in enumerate(model.listOfReactions):  # set reactions to uniform random number and add to y0
                rxn_offset[r] = math.log(random.uniform(0, 1))
                if debug:
                    print("Setting Random number ", rxn_offset[r], " for ", model.listOfReactions[r].name)

            compiled_reactions = {}
            for i, r in enumerate(model.listOfReactions):
                compiled_reactions[r] = compile(model.listOfReactions[r].propensity_function, '<string>',
                                                'eval')
            compiled_rate_rules = {}
            for i, rr in enumerate(model.listOfRateRules):
                compiled_rate_rules[rr] = compile(model.listOfRateRules[rr].expression, '<string>', 'eval')
                
            compiled_inactive_reactions = {}
            
            all_compiled = {'rxns': compiled_reactions, 'rules': compiled_rate_rules, 'inactive_rxns': compiled_inactive_reactions}

            timestep = 0

            while save_time < t:
                while curr_time < save_time:
                    projected_reaction = None
                    tau_step = None
                    tau_j = {}

                    if debug:
                        print("curr_state = {", end='')
                        for i, s in enumerate(model.listOfSpecies):
                            print("'{0}' : {1}, ".format(s, curr_state[s]), end='')
                        print("}")

                    # Salis et al. eq (16)
                    # TODO: this needs to be optimized.  Going too big is expensive, too small is also expensive
                    propensity_sum = 0
                    for i, r in enumerate(model.listOfReactions):
                        propensities[r] = eval(model.listOfReactions[r].propensity_function, curr_state)
                        propensity_sum += propensities[r]
                        if propensities[r] > 0:
                            tau_j[r] = -rxn_offset[r] / propensities[r]
                            if debug:
                                print("Propensity of ", r, " is ", propensities[r], "tau_j is ", tau_j[r])
                            if tau_step is None or tau_j[r] < tau_step:
                                tau_step = max(tau_j[r], 1e-10)
                                projected_reaction = model.listOfReactions[r]
                        else:
                            if debug:
                                print("Propensity of ", r, " is ", propensities[r])

                    if tau_step is None:
                        tau_step = save_time - curr_time

                    if debug:
                        if projected_reaction is None:
                            print("NO projected reaction")
                        else:
                            print("Projected reaction is: ", projected_reaction.name, " at time: ", curr_time + tau_step,
                                  " step size: ", tau_step)

                    #BEGIN NEW TAU SELECTION METHOD
                    g_i = {}    # used for relative error calculation
                    epsilon_i = {}  # relative error allowance of species
                    tau_i = {}  # estimated tau based on depletion of species
                    reactants = []  # a list of all species in the model which act as reactants
                    mu_i = {}   # mu_i for each species
                    sigma_i = {}  # sigma_i squared for each species
                    critical_reactions = []
                    new_tau_step = None
                    n_fires = 2  # if a reaction would deplete a resource in n_fires, it is considered critical

                    #Create list of all reactants
                    for r in model.listOfReactions:
                        reactant_keys = model.listOfReactions[r].reactants.keys()
                        for key in reactant_keys:
                            reactants.append(key)
                    # initialize mean and stand_dev for reactants
                    for r in reactants:
                        mu_i[r] = 0
                        sigma_i[r] = 0

                    critical = False
                    for r in model.listOfReactions:
                        # For each reaction, determine if critical
                        for reactant in model.listOfReactions[r].reactants:
                            # if species pop / state change <= threshold set critical and break
                            if curr_state[str(reactant)] / model.listOfReactions[r].reactants[reactant] <= n_fires:
                                critical = True
                                critical_reactions.append(r)
                    if not critical:
                        for r in model.listOfReactions:
                            for reactant in model.listOfReactions[r].reactants:
                                g_i[reactant] = 3 + (1 / (curr_state[str(reactant)] - 1)) + (
                                        2 / (curr_state[str(reactant)] - 2))  # Cao, Gillespie, Petzold 27.iii
                                epsilon_i[reactant] = self.epsilon / g_i[reactant]  # Cao, Gillespie, Petzold 27
                                mu_i[reactant] += model.listOfReactions[r].reactants[reactant] * propensities[
                                    r]  # Cao, Gillespie, Petzold 29a
                                sigma_i[reactant] += model.listOfReactions[r].reactants[reactant] ** 2 * propensities[
                                    r]  # Cao, Gillespie, Petzold 29b
                            for r in reactants:
                                if mu_i[r] > 0:
                                    # Cao, Gillespie, Petzold 33
                                    tau_i[r] = min((max(epsilon_i[r] * curr_state[str(r)], 1) / mu_i[r]),
                                                   # Cao, Gillespie, Petzold 32A
                                                   (max(epsilon_i[r] * curr_state[str(r)], 1) ** 2 / sigma_i[
                                                       r]))  # Cao, Gillespie, Petzold 32B
                                    
                                    if new_tau_step is None or tau_i[
                                        r] < new_tau_step:  # set smallest tau from non-critical reactions
                                        new_tau_step = tau_i[r]

                    if new_tau_step is not None and new_tau_step < (save_time - curr_time): # if curr+new_tau < save_time, use new_tau
                        tau_step = max(new_tau_step, 1e-10)

                    if profile:
                        steps_taken.append(tau_step)

                    # END NEW TAU SELECTION METHOD
                    mn, sd, CV = self.calculate_statistics(model, propensities, curr_state, tau_step, det_spec, dependencies)
                    deterministic_reactions = self.flag_det_reactions(model, det_spec, det_rxn, dependencies)

                    if debug:
                        print('Calculating mean, standard deviation at time {0}'.format((curr_time + tau_step)))
                        print('mean: {0}'.format(mn))
                        print('standard deviation: {0}'.format(sd))
                        print('CV: {0}'.format(CV))
                        print('det_spec: {0}'.format(det_spec))
                        print('det_rxn: {0}'.format(det_rxn))
                         
                    self.toggle_reactions(model, all_compiled, deterministic_reactions, dependencies, curr_state, rxn_offset, det_spec)
                    active_rr = compiled_rate_rules[deterministic_reactions]

                    y0 = [0] * (len(compiled_reactions) + len(active_rr))
                    for i, spec in enumerate(active_rr):
                        y0[i] = curr_state[spec]
                    for i, rxn in enumerate(compiled_reactions):
                        y0[i+len(active_rr)] = rxn_offset[rxn]
                    
                    prev_y0 = y0.copy()
                    prev_curr_state = curr_state.copy()
                    prev_curr_time = curr_time

                    loop_cnt = 0
                    while True:
                        loop_cnt += 1
                        if loop_cnt > 100:
                            raise Exception("Loop over __get_reactions() exceeded loop count")

                        reactions, y0, curr_state, curr_time = self.__get_reactions(
                            tau_step, curr_state, y0, model, curr_time, save_time, propensities, compiled_reactions,
                            active_rr, rxn_offset, debug)


                        # Update curr_state with the result of the SSA reaction that fired
                        species_modified = {}
                        for i, r in enumerate(compiled_reactions):
                            if reactions[r] > 0:
                                for reactant in model.listOfReactions[r].reactants:
                                    species_modified[str(reactant)] = True
                                    curr_state[str(reactant)] -= model.listOfReactions[r].reactants[reactant] * reactions[r]
                                for product in model.listOfReactions[r].products:
                                    species_modified[str(product)] = True
                                    curr_state[str(product)] += model.listOfReactions[r].products[product] * reactions[r]
                                    
                        neg_state = False
                        for s in species_modified.keys():
                            if curr_state[s] < 0:
                                neg_state = True
                                if debug:
                                    print("Negative state detected: curr_state[{0}]= {1}".format(s, curr_state[s]))
                        if neg_state:
                            steps_rejected +=1
                            if debug:
                                print("\trxn={0}".format(reactions))
                            y0 = prev_y0.copy()
                            curr_state = prev_curr_state.copy()
                            curr_time = prev_curr_time
                            tau_step = tau_step / 2
                            if debug:
                                print("Resetting curr_state[{0}]= {1}".format(s, curr_state[s]))
                            if debug:
                                print("\tRejecting step, taking step of half size, tau_step={0}".format(tau_step))
                        else:
                            break  # breakout of the while True
                if profile:
                    steps_taken.append(tau_step)
                if show_labels:
                    results['time'].append(save_time)
                    for i, s in enumerate(model.listOfSpecies):
                        results[s].append(curr_state[s])
                else:
                    trajectories[trajectory][timestep][0] = save_time
                    for i, s in enumerate(model.listOfSpecies):
                        trajectories[trajectory][timestep][i + 1] = curr_state[s]


                save_time += increment
                timestep += 1

            if show_labels:
                trajectories.append(results)
            if profile:
                print(steps_taken)
                print("Total Steps Taken: ", len(steps_taken))
                print("Total Steps Rejected: ", steps_rejected)

        return trajectories
